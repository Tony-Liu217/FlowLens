"""Fast offline dependency check for a helpful double-click startup failure."""
import importlib.util
import sys

required = ['openpyxl','xlrd','pdfplumber','psutil','rapidocr','pypdfium2','cv2','numpy','PIL','onnxruntime']
missing = [name for name in required if importlib.util.find_spec(name) is None]
if sys.version_info < (3,10) or missing:
    print('运行环境不完整，请按 README 创建虚拟环境并安装 requirements.txt。')
    if missing:
        print('缺少依赖：' + ', '.join(missing))
    sys.exit(1)
print('本地读取与 OCR 运行环境检查通过。')
