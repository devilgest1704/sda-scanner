"""V25 runtime entrypoint.

Keeps the established V25 tuning core isolated while installing the final
read-only wallet/position consistency layer after all dashboard patches.
"""

from v25_runtime_tuning_core import patch as _core_patch


def patch():
    hunter = _core_patch()
    try:
        import telegram_dashboard as dashboard
        import dashboard_data_sync
        dashboard_data_sync.patch_dashboard(dashboard)
    except Exception as exc:
        print(f"Dashboard data sync patch error: {exc}")
    return hunter
