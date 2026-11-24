"""Codex-style parallel-safe tool batches, with barriers around other tools."""

from concurrent.futures import ThreadPoolExecutor

# These tools have no workspace/process mutations and do not ask questions.
PARALLEL_SAFE = frozenset({"read_file", "list_files", "search", "current_time", "get_context_remaining"})


def execute_batches(calls, execute, workers):
    index = 0
    while index < len(calls):
        call = calls[index]
        if workers < 2 or call["function"]["name"] not in PARALLEL_SAFE:
            yield call, execute(call)
            index += 1
            continue
        end = index
        while end < len(calls) and calls[end]["function"]["name"] in PARALLEL_SAFE:
            end += 1
        group = calls[index:end]
        # Results stay in the provider's call order, even if events finish out of order.
        with ThreadPoolExecutor(max_workers=min(workers, len(group)), thread_name_prefix="pycoder-tool") as pool:
            for item, result in zip(group, pool.map(execute, group)):
                yield item, result
        index = end
