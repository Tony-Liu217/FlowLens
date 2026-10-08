"""One-time model setup, without reading or uploading any statements.

Run with the same Python environment as the app. RapidOCR may download missing
model files here; the application's separate OCR worker blocks network access.
"""
import sys


def main():
    try:
        from rapidocr import RapidOCR
        print('正在准备本地 OCR 模型；首次运行可能联网下载模型，不读取账单。', flush=True)
        RapidOCR(params={
            'EngineConfig.onnxruntime.intra_op_num_threads': 4,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'Global.log_level': 'warning',
            'Global.max_side_len': 2400,
        })
    except Exception as exc:
        print(f'OCR 模型准备失败（{type(exc).__name__}）。请检查依赖、网络和环境写入权限后重试。', file=sys.stderr)
        return 1
    print('OCR 模型已就绪。后续账单识别在本机进行，无需外部 AI 密钥。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
