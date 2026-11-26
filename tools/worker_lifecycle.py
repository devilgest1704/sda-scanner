"""Small, deterministic worker-lifecycle policy shared by runner and tests."""
import sys

DEFAULT_MAX_RUNTIME_SECONDS = 4 * 60 * 60


def should_rotate(started_at, now, max_runtime=DEFAULT_MAX_RUNTIME_SECONDS):
    """Return true once a worker has completed its configured safe lifetime."""
    if isinstance(started_at, bool) or not isinstance(started_at, int):
        raise ValueError("started_at must be an integer epoch timestamp")
    if isinstance(now, bool) or not isinstance(now, int):
        raise ValueError("now must be an integer epoch timestamp")
    if isinstance(max_runtime, bool) or not isinstance(max_runtime, int) or max_runtime <= 0:
        raise ValueError("max_runtime must be a positive integer")
    age = now - started_at
    if age < 0:
        raise ValueError("worker start timestamp is in the future")
    return age >= max_runtime


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print("usage: python -m tools.worker_lifecycle STARTED_AT NOW MAX_RUNTIME", file=sys.stderr)
        return 2
    try:
        started_at, now, max_runtime = (int(value) for value in argv)
        rotate = should_rotate(started_at, now, max_runtime)
    except (TypeError, ValueError):
        print("invalid worker lifecycle timestamps", file=sys.stderr)
        return 2
    print("ROTATE" if rotate else "CONTINUE")
    return 0 if rotate else 1


if __name__ == "__main__":
    raise SystemExit(main())
