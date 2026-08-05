from .models import RouterRuntimeState, RouterSnapshot
from .supervisor import RuijieCollectorSupervisor

# Compatibility alias for older imports.
CollectorStatus = RouterRuntimeState

__all__ = ["CollectorStatus", "RouterRuntimeState", "RouterSnapshot", "RuijieCollectorSupervisor"]
