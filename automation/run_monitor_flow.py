"""Cloud Run entry point for the OpenPapers monitor Prefect flow."""

import os

from automation.prefect_flows import monitor_flow


def main() -> None:
    bucket = os.environ.get("OPENPAPERS_MONITOR_BUCKET")
    if not bucket:
        raise ValueError("OPENPAPERS_MONITOR_BUCKET is required")
    monitor_flow(
        bucket_name=bucket,
        require_remote_state=True,
        state_path="/tmp/openpapers-data/monitor/state.sqlite3",
    )


if __name__ == "__main__":
    main()
