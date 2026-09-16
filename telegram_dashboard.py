# V26 dashboard patch must run AFTER legacy V21 compatibility patch, because
# strategy_v21.patch_engine() otherwise overwrites the V26 presentation hooks.
import v26_dashboard_patch as _v26_dashboard_patch
_v26_dashboard_patch.patch_dashboard(sys.modules[__name__])

if __name__ == "__main__": run()