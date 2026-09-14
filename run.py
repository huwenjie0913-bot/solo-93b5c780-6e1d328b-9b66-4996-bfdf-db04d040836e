"""本地启动入口：python run.py"""
import os
import sys

# 兼容将依赖装在项目内 .pylibs 的环境
_LIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pylibs",
                     "lib", f"python{sys.version_info.major}.{sys.version_info.minor}",
                     "site-packages")
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
