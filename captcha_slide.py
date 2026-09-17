"""booking.fudan.edu.cn 滑块验证码（AJ-Captcha 协议）自动求解。

取题 GET  /site/resource/slide-captcha?id=<resource_id>
     -> {originalImageBase64, jigsawImageBase64, secretKey, token}
校验 POST /site/resource/slide-captcha-check      （必须 form 编码）
     token + pointJson，其中
     pointJson = base64( AES-128-ECB + PKCS7( key=secretKey, 明文 '{"x":X,"y":5}' ) )
     X = 缺口左边界在**原图**中的像素坐标，y 恒为 5（不是缺口的 y）。
     成功返回 d.success=true；token 一次性，失败作废。
提交预约时回填 captcha = json.dumps({"token": token, "pointJson": pointJson})。

求解：缺口是原图上的一圈白色描边，用「到白边的距离场」+ 拼图块轮廓带做 chamfer
匹配（FFT 相关实现），取平均距离最小的窗口，单次约几十毫秒。

对外约定：SlideCaptcha.solve() 只会抛 CaptchaError（网络层异常仍按 requests 原样抛出），
调用方捕获 CaptchaError 即可，不需要再猜其他异常类型。

自测（用 .local 里缓存的会话）：python captcha_slide.py --bench 20
"""

import argparse
import base64
import io
import json
import time
from pathlib import Path

import numpy as np
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image
from scipy.ndimage import distance_transform_edt

BASE = "https://booking.fudan.edu.cn/reservation"
GEN_URL = BASE + "/site/resource/slide-captcha"
CHK_URL = BASE + "/site/resource/slide-captcha-check"
DEFAULT_SESSION = Path(__file__).resolve().parent / ".local" / "soccer_session_v2.json"
DEFAULT_RESOURCE = 1029
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
WHITE_THRESHOLD = 190   # 判定「白色描边」的亮度阈值（min(RGB)）
FIXED_Y = 5             # 服务端约定：y 恒为 5


class CaptchaError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 图像求解
# --------------------------------------------------------------------------

def _distance_to_white(white):
    """每个像素到最近白色像素的欧氏距离。"""
    return distance_transform_edt(~white).astype(np.float32)


def _window_sums(image, mask):
    """valid 模式：每个滑动窗口内 image*mask 之和，形状 (H-h+1, W-w+1)，用 FFT。"""
    H, W = image.shape
    kernel = np.zeros_like(image)
    kernel[:mask.shape[0], :mask.shape[1]] = mask[::-1, ::-1]
    out = np.fft.irfft2(np.fft.rfft2(image) * np.fft.rfft2(kernel), image.shape)
    return out[mask.shape[0] - 1:H, mask.shape[1] - 1:W]


def solve_offset(original_b64, jigsaw_b64):
    """算缺口左边界 x。返回 (x, 残差)；残差是拼图块轮廓到白边的平均距离（像素）。"""
    bg = np.asarray(Image.open(io.BytesIO(base64.b64decode(original_b64))).convert("RGB"), dtype=np.int16)
    piece = np.asarray(Image.open(io.BytesIO(base64.b64decode(jigsaw_b64))).convert("RGBA"))
    rows, cols = np.where(piece[:, :, 3] > 100)
    if not len(rows):
        raise CaptchaError("拼图块是空的")
    mask = piece[rows.min():rows.max() + 1, cols.min():cols.max() + 1, 3] > 100
    if mask.shape[0] >= bg.shape[0] or mask.shape[1] >= bg.shape[1]:
        raise CaptchaError("拼图块尺寸异常")
    # 轮廓带：边界 2 像素
    inner = mask.copy()
    for _ in range(2):
        eroded = inner.copy()
        eroded[1:, :] &= inner[:-1, :]
        eroded[:-1, :] &= inner[1:, :]
        eroded[:, 1:] &= inner[:, :-1]
        eroded[:, :-1] &= inner[:, 1:]
        inner = eroded
    band = (mask & ~inner).astype(np.float32)
    distance = _distance_to_white(bg.min(axis=2) > WHITE_THRESHOLD)
    scores = _window_sums(distance, band) / float(band.sum())
    flat = int(scores.reshape(-1).argmin())
    y, x = divmod(flat, scores.shape[1])
    return int(x), float(scores[y, x])


# --------------------------------------------------------------------------
# 协议实现
# --------------------------------------------------------------------------

def encrypt_point(x, secret_key, y=FIXED_Y):
    """AES-128-ECB + PKCS7，key = secretKey，明文 {"x":X,"y":5}。"""
    raw = json.dumps({"x": int(x), "y": int(y)}, separators=(",", ":")).encode()
    pad = 16 - len(raw) % 16
    encryptor = Cipher(algorithms.AES(secret_key.encode()), modes.ECB()).encryptor()
    return base64.b64encode(encryptor.update(raw + bytes([pad]) * pad) + encryptor.finalize()).decode()


class SlideCaptcha:
    """取题 → 求解 → 校验；通过的凭据只能用于一次提交。"""

    def __init__(self, session, resource_id=DEFAULT_RESOURCE, retries=3):
        self.http, self.resource_id, self.retries = session, resource_id, retries

    def solve(self):
        """返回 ({"token","pointJson"}, x, 残差)。

        约定：任何失败都以 CaptchaError 抛出，调用方只需捕获这一种异常
        （解析失败、字段改名、非 JSON 响应都在这里统一包装）。
        """
        last = "未知错误"
        for _ in range(self.retries):
            try:
                body = self.http.get(GEN_URL, params={"id": self.resource_id}, timeout=(10, 30)).json()
                if not isinstance(body, dict):
                    raise CaptchaError("取题响应不是 JSON 对象")
                challenge = body.get("d")
                if body.get("e") != "OK" or not isinstance(challenge, dict):
                    raise CaptchaError(f"取题失败：{str(body)[:160]}")
                original = challenge["originalImageBase64"]
                jigsaw = challenge["jigsawImageBase64"]
                secret = challenge["secretKey"]
                token = challenge["token"]
                x, residual = solve_offset(original, jigsaw)
                point_json = encrypt_point(x, secret)
                check = self.http.post(CHK_URL, timeout=(10, 30),
                                       data={"token": token, "pointJson": point_json}).json()
                if isinstance(check, dict) and isinstance(check.get("d"), dict) and check["d"].get("success") is True:
                    return {"token": token, "pointJson": point_json}, x, residual
                last = f"位置 {x} 校验未通过（残差 {residual:.2f}）"
            except CaptchaError as exc:
                last = str(exc)
            except (ValueError, KeyError, TypeError) as exc:
                # 非 JSON 响应、字段改名、图片损坏等：包装成 CaptchaError 后重试。
                last = f"{type(exc).__name__}: {str(exc)[:120]}"
        raise CaptchaError(f"连续 {self.retries} 张验证码都没过：{last}")


def default_session():
    """优先用足球自动化里的会话文件，避免两处路径各写一份而漂移。"""
    try:
        from automations import soccer
        return Path(soccer.STATE)
    except (ImportError, AttributeError):
        return DEFAULT_SESSION


def load_session(path=None):
    """读 .local 里缓存的登录会话，构造 requests.Session。"""
    state = json.loads(Path(path or default_session()).read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers.update({"User-Agent": state.get("user_agent", USER_AGENT),
                            "Referer": BASE + "/fe/site/reservationInfo?id=%d" % DEFAULT_RESOURCE,
                            "Origin": "https://booking.fudan.edu.cn"})
    for cookie in state.get("cookies", []):
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=int, default=0, help="连续求解 N 张，统计成功率与耗时")
    parser.add_argument("--session", type=Path, default=None, help="默认用足球自动化里的会话文件")
    parser.add_argument("--resource", type=int, default=DEFAULT_RESOURCE)
    args = parser.parse_args()

    if not args.bench:
        parser.print_help()
        return

    solver = SlideCaptcha(load_session(args.session), args.resource)
    ok, times, residuals = 0, [], []
    for index in range(1, args.bench + 1):
        started = time.perf_counter()
        try:
            solved, x, residual = solver.solve()
        except CaptchaError as exc:
            times.append(time.perf_counter() - started)
            print(f"[{index}] 失败：{exc}")
            continue
        elapsed = time.perf_counter() - started
        ok += 1
        times.append(elapsed)
        residuals.append(residual)
        print(f"[{index}] 通过 x={x:3d} 残差={residual:.2f}px 耗时={elapsed * 1000:.0f}ms")
    if times:
        print(f"\n成功 {ok}/{len(times)}  "
              f"耗时 min/avg/max = {min(times) * 1000:.0f}/"
              f"{sum(times) / len(times) * 1000:.0f}/{max(times) * 1000:.0f}ms")
    if residuals:
        print("残差 max = %.2fpx" % max(residuals))


if __name__ == "__main__":
    main()
