import random
import shutil
import time
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast

import numpy
import regex as re
import torch

from . import generation as gen
from . import utils
from .analyzer import JdtLspAnalyzer, Message
from .config import MetaConfig, RepairConfig
from .d4j import Bug, Change, Defects4J
from .lsp import TextFile, spec
from .model import CodeT5ForRealm, CodeT5Large
from .report import Report
from .results import HunkRepairResult, RepairResult

DATA_DIR = Path(".lsp_data")


def wait_until_all_analyzers_free(
    realm_conns: list[Connection],
    max_waiting_time: float = 20,
    free_check_time: float = 1.0,
):
    batch_is_free = [False] * len(realm_conns)
    start_time = time.perf_counter()
    print("Waiting until all analyzers are free...")
    while time.perf_counter() < start_time + max_waiting_time:
        for idx, connection in enumerate(realm_conns):
            if not batch_is_free[idx]:
                connection.send(
                    Message(True, JdtLspAnalyzer.is_free.__name__, free_check_time)
                )

        for idx, connection in enumerate(realm_conns):
            if not batch_is_free[idx]:
                is_free = connection.recv()
                batch_is_free[idx] = is_free
        print("Elapsed:", time.perf_counter() - start_time)
        if all(batch_is_free):
            print("All analyzers are free:", time.perf_counter() - start_time)
            break


def get_buggy_hunk_start_end_indices_and_positions(
    text_file: TextFile, change: Change
) -> tuple[int, int, spec.Position, spec.Position]:
    start = change.start - 1
    end = start + len(change.removed_lines)
    start_pos = text_file.refine_index(start, 0)
    end_pos = text_file.refine_index(end, 0)

    start_index = text_file.form_index(start, 0)
    end_index = text_file.form_index(end, 0)
    return start_index, end_index, start_pos, end_pos


def remove_buggy_hunk(text_file: TextFile, change: Change) -> tuple[str, str]:
    """Modifies `text_file` and returns the prefix and the suffix"""
    (
        start_index,
        end_index,
        _,
        _,
    ) = get_buggy_hunk_start_end_indices_and_positions(text_file, change)
    prefix_start = 0
    suffix_end = len(text_file.content)
    prefix = text_file.content[prefix_start:start_index]
    # "\n" is important as we need a blank place for generation
    suffix = "\n" + text_file.content[end_index:suffix_end]

    # prefix(\n)
    # insertion(\n)
    # <cursor:infill>
    # (\n)suffix
    text_file.modify(start_index, end_index, "\n")
    # text_file.change(
    #     [
    #         cast(
    #             spec.EntireDocumentChange,
    #             {"text": "\n", "range": {"start": start_pos, "end": end_pos}},
    #         )
    #     ]
    # )

    text_file.move_cursor(start_index)
    if start_index != 0:
        assert prefix.endswith("\n")
        assert text_file.content[text_file.cursor - 1] == "\n"
        assert text_file.content[text_file.cursor] == "\n"

    return prefix, suffix


class Repairer:
    def __init__(
        self,
        config: MetaConfig,
        model: CodeT5ForRealm,
        d4j: Defects4J,
        active_connection_analyzer_pairs: list[tuple[Connection, JdtLspAnalyzer]],
    ) -> None:
        self.config = config
        self.model = model
        self.d4j = d4j
        self.active_connection_analyzer_pairs = active_connection_analyzer_pairs

    @staticmethod
    def init(config: MetaConfig, pre_allocate: bool) -> "Repairer":
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR)
        # report = Report.create(report_dir, config)
        model = CodeT5Large.init().to(utils.DEVICE)  # type: ignore # noqa
        if pre_allocate:
            print("Doing pre-allocation..")
            model.pre_allocate()
            print("Done.")
        return Repairer(config, model, config.d4j(), [])

    def server_cmd_maker(self) -> list[str]:
        # IMPORTANT: -data dir should be DIFFERENT for different analyzers!!!
        return self.config.language_server_cmd + [
            "-data",
            str(DATA_DIR / str(next(utils.COUNTER))),
        ]

    def fix_seed(self):
        torch.manual_seed(self.config.seed)
        numpy.random.seed(self.config.seed)
        random.seed(self.config.seed)

    # This `Report` type is too big. In general, we can lift the state up by intializing
    # a smaller object with a `didChange` method. This is what React does.
    def repair(self, report: Report):
        """Add results to `report` given `config`"""
        self.fix_seed()
        assert report.repair_result is not None
        repair_result = report.repair_result
        config = repair_result.repair_config
        pattern = re.compile(config.bug_pattern)
        bugs_considered = (
            self.d4j.single_hunk_bugs if config.hunk_only else self.d4j.all_bugs
        )
        bugs_to_repair = {
            bug_id: bug
            for bug_id, bug in bugs_considered.items()
            if pattern.fullmatch(bug_id) is not None
            # Unicode error
            and bug_id != "Lang-25"
        }
        for bug_id, bug in bugs_to_repair.items():
            gen.CHART_11 = bug_id == "Chart-11"
            self.repair_bug(report, bug_id, bug)

    def clean_up(self):
        for connection, _ in self.active_connection_analyzer_pairs:
            connection.send(None)
        for _, analyzer in self.active_connection_analyzer_pairs:
            analyzer.join()
        self.active_connection_analyzer_pairs.clear()

    def repair_bug(self, report: Report, bug_id: str, bug: Bug):
        try:
            self.repair_bug_no_cleanup(report, bug_id, bug)
        finally:
            self.clean_up()
            print("Cleaned up")

    def repair_bug_no_cleanup(self, report: Report, bug_id: str, bug: Bug):
        assert report.repair_result is not None
        config = report.repair_result.repair_config
        print("Repair", bug_id)
        self.d4j.checkout(bug_id)

        def init_analyzers() -> tuple[list[Connection], list[TextFile]]:
            if not config.method.is_plain():
                connection_pairs = cast(
                    list[tuple[Connection, Connection]],
                    [Pipe(duplex=True) for _ in range(config.batch_size)],
                )
                connection_analyzer_pairs = [
                    (
                        client_conn,
                        JdtLspAnalyzer(
                            analyzer_conn,
                            self.server_cmd_maker(),
                            Path(bug.proj_path),
                            self.model,
                            str(self.config.java8_home),
                        ),
                    )
                    for analyzer_conn, client_conn in connection_pairs
                ]
                connections = [
                    connection for connection, _ in connection_analyzer_pairs
                ]
                for connection, analyzer in connection_analyzer_pairs:
                    analyzer.start()
                    self.active_connection_analyzer_pairs.append((connection, analyzer))
                for connection in connections:
                    connection.send(Message(False, JdtLspAnalyzer.init.__name__))
            else:
                meaning_less = utils.Meaningless
                connection_analyzer_pairs = cast(
                    list[tuple[Connection, JdtLspAnalyzer]],
                    [(meaning_less, meaning_less)] * config.batch_size,
                )
                connections = cast(list[Connection], [meaning_less] * config.batch_size)

            # Buggy text files
            base_path = Path(bug.proj_path).parent.absolute()
            proj_path = Path(bug.proj_path).relative_to(base_path)
            buggy_text_files = [
                TextFile.read(base_path, proj_path / buggy_file.path)
                for buggy_file in bug.buggy_files
            ]

            # Initialize each buggy file for LSP
            if not config.method.is_plain():
                assert isinstance(connections, list)
                for connection in connections:
                    for buggy_text_file in buggy_text_files:
                        connection.send(
                            Message(
                                False, JdtLspAnalyzer.open.__name__, buggy_text_file
                            )
                        )
                wait_until_all_analyzers_free(connections)
            return connections, buggy_text_files

        # Ready to repair
        analyzers_initialized = False
        for hunk_idx, buggy_file, change in bug.iter_hunks():
            result_dict = report.repair_result.result_dict
            f_idx, h_idx = hunk_idx

            def get_files() -> list[list[HunkRepairResult]] | None:
                return result_dict.get(bug_id)

            def get_hunks(
                files: list[list[HunkRepairResult]],
            ) -> list[HunkRepairResult] | None:
                return files[f_idx] if f_idx < len(files) else None

            def get_n_samples(hunks: list[HunkRepairResult]) -> int | None:
                return len(hunks[h_idx].results) if h_idx < len(hunks) else None

            # FP experiment
            n_already_generated = utils.bind_optional(
                utils.bind_optional(get_files(), get_hunks), get_n_samples
            )
            if (
                n_already_generated is not None
                and n_already_generated == config.n_samples
            ):
                print(f"Skipping {bug_id} {hunk_idx}")
                continue
            if not analyzers_initialized:
                # Only intialized once
                connections, buggy_text_files = init_analyzers()
                analyzers_initialized = True
            buggy_text_file = buggy_text_files[f_idx]
            (
                buggy_hunk_start_index,
                buggy_hunk_end_index,
                _,
                _,
            ) = get_buggy_hunk_start_end_indices_and_positions(buggy_text_file, change)
            text_file = buggy_text_file.copy()
            buggy_hunk = "".join(change.removed_lines)
            buggy_hunk = buggy_hunk[:-1] if buggy_hunk.endswith("\n") else buggy_hunk
            print("Buggy hunk:", repr(buggy_hunk))
            prefix, suffix = remove_buggy_hunk(text_file, change)
            lm_context = gen.LMContext(
                self.model, prefix, suffix, config.lm_inference_config
            )
            synthesizer = gen.Synthesizer(
                lm_context, connections, text_file, config.method, buggy_hunk
            )
            n_samples = config.n_samples - (
                0 if n_already_generated is None else n_already_generated
            )
            torch.manual_seed(0)
            numpy.random.seed(0)
            random.seed(0)
            for idx in range(n_samples):
                print("Hunk index:", hunk_idx)
                print("Repair index:", idx)

                synthesis_result_batch = synthesizer.synthesize()
                assert len(synthesis_result_batch.results) == config.batch_size
                for result in synthesis_result_batch.results:
                    if result.hunk is not None:
                        # assert (
                        #     buggy_text_file.content[:buggy_hunk_start_index]
                        #     + success.hunk
                        #     + "\n"
                        #     + buggy_text_file.content[buggy_hunk_end_index:]
                        # ) == success.patch.content
                        print(result.hunk)
                    else:
                        print(result)
                print("Time cost:", synthesis_result_batch.time_cost)
                buggy_file_path = Path(bug.proj_path) / buggy_file.path
                assert buggy_file_path.exists()
                report.repair_result.add(
                    bug_id,
                    hunk_idx,
                    synthesis_result_batch,
                    buggy_text_file,
                    buggy_hunk_start_index,
                    buggy_hunk_end_index,
                )
                report.save()
                # WARNING: Timeout error, if happend, indicates the TIMEOUT_THRESHOULD is too small (unlikely)
                # or a fatal implementation error!!
                # except TimeoutError:
                #     self.report.report_timeout_error()
