import importlib.util
import os


def load_lima():
    path = os.path.join(os.path.dirname(__file__), "..", "python", "lima.py")
    spec = importlib.util.spec_from_file_location("lima", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
