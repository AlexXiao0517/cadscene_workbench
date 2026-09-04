"""轻量产品工作流状态支持。"""

from .job_status import JobStatusStore, create_job_status

__all__ = ["JobRunner", "JobStatusStore", "create_job_status"]


def __getattr__(name: str):
    if name == "JobRunner":
        from .job_runner import JobRunner

        return JobRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
