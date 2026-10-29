import importlib.util
import os


def load_agps():
    path = os.path.join(os.path.dirname(__file__), "..", "python", "agps.py")
    spec = importlib.util.spec_from_file_location("agps", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
