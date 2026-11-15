"""
VTube Studio 연결 모듈 (vts_client.py)

파이썬 프로그램이 VTube Studio에 접속해서, 캐릭터의 감정에 맞는 표정을 켜고 끕니다.
VTube Studio가 공식으로 제공하는 WebSocket API를 사용하며, 추가 비용은 없습니다.

사용 전 준비:
1. pip install websockets
2. VTube Studio를 켜고, 설정 메인 화면에서 'Allow Plugin API access'를 켠다.
   (기본 주소는 ws://localhost:8001 입니다.)
3. 처음 연결할 때 VTube Studio 화면에 '플러그인 허용' 팝업이 뜨면 Allow를 누른다.
   이후에는 vts_token.txt에 저장된 토큰으로 자동 인증되어 팝업이 다시 뜨지 않습니다.

단독 테스트: python vts_client.py
표정 연결(처음 한 번): python vts_client.py --map
  → 모델의 실제 표정 목록이 번호로 나오고, 10개 감정에 쓸 표정을 골라 연결합니다.
(VTube Studio에서 모델을 불러온 상태로 실행하면, 현재 모델의 표정 목록을 보여주고
 감정 이름을 입력해서 표정이 바뀌는지 직접 확인할 수 있습니다.)
"""

import json
import os
import sys
import time
import uuid

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    # websockets가 설치되지 않아도 메인 프로그램은 계속 동작하도록 한다 (표정만 안 바뀜)
    ws_connect = None

VTS_URL = "ws://localhost:8001"
PLUGIN_NAME = "AraAIVTuber"      # VTube Studio 허용 팝업에 표시되는 이름 (3~32자)
PLUGIN_DEVELOPER = "AraProject"  # 개발자 이름 (3~32자)
TOKEN_FILE = "vts_token.txt"     # 한 번 허락받은 인증 토큰을 저장하는 파일

# 아라의 감정 → VTube Studio 표정 파일(.exp3.json) 이름
# 모델러가 만든 실제 파일 이름과 다르면 오른쪽 값만 고치면 됩니다.
# (단독 테스트 또는 메인 프로그램의 /vts 명령으로 실제 이름을 확인할 수 있어요.)
# None은 '표정을 모두 끈 기본 상태'를 뜻합니다.
EXPRESSION_FILES = {
    "기본": None,
    "미소": "smile.exp3.json",
    "장난": "playful.exp3.json",
    "화남": "angry.exp3.json",
    "당황": "flustered.exp3.json",
    "부끄": "shy.exp3.json",
    "슬픔": "sad.exp3.json",
    "멍": "blank.exp3.json",
    "신남": "excited.exp3.json",
    "졸려": "sleepy.exp3.json",
}

# python vts_client.py --map 으로 만든 매핑 파일. 있으면 위의 기본값보다 우선합니다.
# (그래서 모델의 실제 표정 이름을 몰라도, 번호로 고르기만 하면 연결됩니다.)
MAP_FILE = "expression_map.json"
# 매핑 값이 "hotkey:이름" 형태면 표정 파일이 아니라 VTube Studio의 '모션 재생' 핫키를 실행한다.
# (모델에 표정 파일이 없고 모션만 있을 때 감정을 몸짓으로 표현하기 위한 방법)
HOTKEY_PREFIX = "hotkey:"

MOUTH_PARAM = "MouthOpen"  # VTS 기본 입력 파라미터 (모델의 입 벌림에 연결되어 있음)


def load_map_overrides(path: str = MAP_FILE) -> dict:
    """매핑 파일이 있으면 {감정: 표정 파일 또는 None}을 돌려준다. 잘못된 내용은 무시한다."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as error:
        print(f"[VTS] {path}를 읽지 못해 기본 매핑을 씁니다: {error}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        emotion: (file_name or None)
        for emotion, file_name in data.items()
        if emotion in EXPRESSION_FILES and (file_name is None or isinstance(file_name, str))
    }


EXPRESSION_FILES.update(load_map_overrides())

EXTRA_FILE = "motion_extra.json"  # {"감정": ["hotkey:이름", ...]} 반복할 때 번갈아 쓸 추가 모션


def load_extra_motions(path: str = EXTRA_FILE) -> dict:
    """추가 모션 파일이 있으면 {감정: [모션, ...]}을 돌려준다. 잘못된 내용은 무시한다."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as error:
        print(f"[VTS] {path}를 읽지 못해 추가 모션 없이 진행합니다: {error}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        emotion: [v for v in values if isinstance(v, str) and v]
        for emotion, values in data.items()
        if emotion in EXPRESSION_FILES and isinstance(values, list)
    }


EXTRA_MOTIONS = load_extra_motions()

class VTSError(Exception):
    """VTube Studio가 오류 응답을 보냈을 때 사용하는 예외."""


class VTubeStudioClient:
    def __init__(self, url: str = VTS_URL, token_file: str = TOKEN_FILE):
        self.url = url
        self.token_file = token_file
        self.ws = None
        self._last_fail_time = 0.0  # 재접속을 너무 자주 시도하지 않기 위한 기록

    # ------------------------------------------------------------
    # 연결 / 인증
    # ------------------------------------------------------------
    def is_connected(self) -> bool:
        return self.ws is not None

    def connect(self, verbose: bool = True) -> bool:
        """VTube Studio에 접속하고 인증한다. 성공하면 True."""
        if ws_connect is None:
            if verbose:
                print("[VTS] 'websockets' 라이브러리가 없습니다. pip install websockets 를 먼저 실행하세요.")
            return False
        try:
            self.ws = ws_connect(self.url, open_timeout=3)
            self._authenticate()
            return True
        except Exception as e:
            self._drop_connection()
            self._last_fail_time = time.time()
            if verbose:
                print(f"[VTS] 연결 실패: {e}")
                if not isinstance(e, VTSError):
                    print("      VTube Studio가 켜져 있는지, 설정의 'Allow Plugin API access'가 켜져 있는지 확인하세요.")
            return False

    def _drop_connection(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def _ensure_connected(self) -> bool:
        """끊겨 있으면 다시 연결을 시도한다. 마지막 실패 후 10초 안에는 재시도하지 않는다."""
        if self.ws is not None:
            return True
        if time.time() - self._last_fail_time < 10:
            return False
        if self.connect(verbose=False):
            print("[VTS] 다시 연결되었습니다.")
            return True
        return False

    def _handle_error(self, error: Exception):
        """요청 중 문제가 생겼을 때 안내를 출력한다. 네트워크 문제면 연결을 끊어둔다."""
        if isinstance(error, VTSError):
            print(f"[VTS] 오류: {error}")
            return
        if isinstance(error, TimeoutError):
            print("[VTS] VTube Studio가 응답하지 않습니다.")
        else:
            print("[VTS] VTube Studio와의 연결이 끊어졌습니다. 잠시 후 자동으로 다시 연결을 시도합니다.")
        self._drop_connection()
        self._last_fail_time = time.time()

    # ------------------------------------------------------------
    # 요청 보내기 (VTube Studio API 공통 형식)
    # ------------------------------------------------------------
    def _request(self, message_type: str, data: dict = None, timeout: float = 10) -> dict:
        request_id = uuid.uuid4().hex[:16]
        self.ws.send(json.dumps({
            "apiName": "VTubeStudioPublicAPI",
            "apiVersion": "1.0",
            "requestID": request_id,
            "messageType": message_type,
            "data": data or {},
        }))
        while True:
            response = json.loads(self.ws.recv(timeout=timeout))
            if response.get("requestID") != request_id:
                continue  # 다른 요청의 응답이나 이벤트는 건너뛴다
            if response.get("messageType") == "APIError":
                err = response.get("data", {})
                raise VTSError(f"{err.get('message')} (errorID {err.get('errorID')})")
            return response.get("data", {})

    def _load_token(self):
        if os.path.exists(self.token_file):
            with open(self.token_file, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        return None

    def _save_token(self, token: str):
        with open(self.token_file, "w", encoding="utf-8") as f:
            f.write(token)

    def _authenticate(self):
        identity = {"pluginName": PLUGIN_NAME, "pluginDeveloper": PLUGIN_DEVELOPER}

        # 1) 저장된 토큰이 있으면 그걸로 바로 인증 (팝업 없음)
        token = self._load_token()
        if token:
            result = self._request("AuthenticationRequest", {**identity, "authenticationToken": token})
            if result.get("authenticated"):
                return
            print("[VTS] 저장된 토큰이 유효하지 않아 새로 허락을 받습니다.")

        # 2) 토큰이 없으면 VTube Studio에 허락을 요청 (화면에 팝업이 뜸)
        print("[VTS] VTube Studio 화면의 '플러그인 허용' 팝업에서 Allow를 눌러주세요. (최대 60초)")
        result = self._request("AuthenticationTokenRequest", identity, timeout=60)
        token = result["authenticationToken"]
        self._save_token(token)

        result = self._request("AuthenticationRequest", {**identity, "authenticationToken": token})
        if not result.get("authenticated"):
            raise VTSError(result.get("reason", "인증에 실패했습니다."))

    # ------------------------------------------------------------
    # 표정 제어
    # ------------------------------------------------------------
    def list_hotkeys(self) -> list:
        """현재 모델에 설정된 핫키를 [(이름, 종류, 핫키ID, 파일), ...] 로 돌려준다.
        종류 예: TriggerAnimation(모션 한 번 재생), ToggleExpression(표정 켜고 끄기)"""
        if not self._ensure_connected():
            return []
        try:
            data = self._request("HotkeysInCurrentModelRequest")
            return [(h.get("name", ""), h.get("type", ""), h.get("hotkeyID", ""), h.get("file", ""))
                    for h in data.get("availableHotkeys", [])]
        except Exception as e:
            self._handle_error(e)
            return []

    def trigger_hotkey(self, name_or_id: str) -> bool:
        """핫키를 이름(대소문자 구분 없음) 또는 ID로 실행한다."""
        if not self._ensure_connected():
            return False
        try:
            self._request("HotkeyTriggerRequest", {"hotkeyID": name_or_id})
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def list_expressions(self) -> list:
        """현재 모델의 표정 목록을 [(파일명, 켜짐여부), ...] 로 돌려준다."""
        if not self._ensure_connected():
            return []
        try:
            state = self._request("ExpressionStateRequest", {"details": False})
            return [(e["file"], bool(e.get("active"))) for e in state.get("expressions", [])]
        except Exception as e:
            self._handle_error(e)
            return []

    def set_emotion(self, emotion: str, variant: int = 0) -> bool:
        """감정 이름에 해당하는 표정을 켜고, 우리가 관리하는 다른 표정은 끈다.
        (사용자가 직접 켠 안경 같은 다른 표정은 건드리지 않는다.)
        성공하면 True, VTube Studio에 연결할 수 없거나 표정이 없으면 False."""
        if emotion not in EXPRESSION_FILES:
            print(f"[VTS] 알 수 없는 감정 '{emotion}' (사용 가능: {', '.join(EXPRESSION_FILES)})")
            return False
        choices = [EXPRESSION_FILES[emotion]] + EXTRA_MOTIONS.get(emotion, [])
        target = choices[variant % len(choices)]
        managed = {f for f in list(EXPRESSION_FILES.values()) + [v for vs in EXTRA_MOTIONS.values() for v in vs]
                   if f and not f.startswith(HOTKEY_PREFIX)}
        if target and target.startswith(HOTKEY_PREFIX):
            # 모션 핫키: 우리가 켜둔 표정은 끄고, 모션을 한 번 재생한다
            if not self.apply_expression(None, managed):
                return False
            return self.trigger_hotkey(target[len(HOTKEY_PREFIX):])
        return self.apply_expression(target, managed)

    def apply_expression(self, target, managed: set) -> bool:
        """target 표정(None이면 표정을 모두 끈 기본 상태)을 켜고, managed에 속한 다른 표정은 끈다."""
        if not self._ensure_connected():
            return False
        try:
            state = self._request("ExpressionStateRequest", {"details": False})
            if not state.get("modelLoaded", True):
                print("[VTS] VTube Studio에 모델이 불러와져 있지 않습니다.")
                return False
            expressions = {e["file"]: e for e in state.get("expressions", [])}

            if target and target not in expressions:
                print(f"[VTS] 현재 모델에 '{target}' 표정이 없습니다. "
                      "python vts_client.py --map 으로 이 모델의 표정에 맞게 연결하세요.")
                return False

            # 이전에 켜둔 우리 표정은 끄고
            for file_name, info in expressions.items():
                if info.get("active") and file_name in managed and file_name != target:
                    self._request("ExpressionActivationRequest",
                                  {"expressionFile": file_name, "active": False})
            # 새 표정을 켠다
            if target and not expressions[target].get("active"):
                self._request("ExpressionActivationRequest",
                              {"expressionFile": target, "fadeTime": 0.25, "active": True})
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def set_mouth(self, value: float) -> bool:
        """입 벌림 값(0~1)을 VTube Studio에 넣는다. 1초 안에 다시 보내지 않으면 VTS가 값을 놓는다."""
        if not self._ensure_connected():
            return False
        try:
            self._request("InjectParameterDataRequest", {
                "mode": "set",
                "parameterValues": [{"id": MOUTH_PARAM, "value": max(0.0, min(1.0, value))}],
            }, timeout=2)
            return True
        except Exception as e:
            self._handle_error(e)
            return False

    def close(self):
        """종료 전에 표정을 기본 상태로 되돌리고 연결을 닫는다.
        (API로 켠 표정은 프로그램이 꺼져도 남아있을 수 있어서 반드시 정리한다.)"""
        if self.ws is None:
            return
        try:
            self.set_emotion("기본")
        except Exception:
            pass
        self._drop_connection()


# ------------------------------------------------------------
# 단독 테스트: python vts_client.py
# ------------------------------------------------------------
def _interactive_test():
    client = VTubeStudioClient()
    if not client.connect():
        return
    print("[VTS] 연결 및 인증 완료\n")

    expressions = client.list_expressions()
    print(f"현재 모델의 표정 {len(expressions)}개:")
    for file_name, active in expressions:
        print(f"  - {file_name}" + ("  (켜짐)" if active else ""))

    hotkeys = client.list_hotkeys()
    print(f"\n현재 모델의 핫키 {len(hotkeys)}개:")
    for name, kind, _id, file_name in hotkeys:
        print(f"  - {name} ({kind}{', ' + file_name if file_name else ''})")

    model_files = {f for f, _ in expressions}
    missing = [(emo, f) for emo, f in EXPRESSION_FILES.items() if f and f not in model_files]
    if missing:
        print("\n[참고] 아래 감정은 현재 모델에서 같은 이름의 표정 파일을 찾지 못했습니다.")
        print("       python vts_client.py --map 으로 이 모델의 실제 표정을 번호로 골라 연결하세요.")
        for emo, f in missing:
            print(f"  - {emo}: {f}")

    print("\n감정 이름을 입력하면 해당 표정을 켭니다. (예: 부끄)  종료: exit")
    while True:
        try:
            text = input("감정> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in ("exit", "quit"):
            break
        if text:
            print("  → 적용됨" if client.set_emotion(text) else "  → 적용하지 못했습니다")
    client.close()


def run_mapping(client=None, path: str = MAP_FILE):
    """모델의 표정 파일과 모션 핫키를 번호로 보여주고, 감정마다 쓸 것을 직접 고르게 한다.
    결과는 expression_map.json에 저장되고, 다음 실행부터 자동으로 적용된다.
    (모델에 표정 파일이 없어도 '모션 재생' 핫키가 있으면 그걸로 감정을 표현할 수 있다.)"""
    client = client or VTubeStudioClient()
    if not client.is_connected() and not client.connect():
        return

    expressions = [file_name for file_name, _ in client.list_expressions()]
    # 모션 핫키만 고를 수 있게 한다. (표정 토글 핫키는 표정 파일과 중복이고, 켜고 끄는 상태 관리가 꼬이기 쉬워서 제외)
    motions = [(name, file_name) for name, kind, _id, file_name in client.list_hotkeys()
               if kind == "TriggerAnimation" and name]
    if not expressions and not motions:
        print("현재 모델에서 표정 파일도, '모션 재생' 핫키도 찾지 못했습니다.")
        print("VTube Studio에 모델이 불러와져 있는지 확인하세요. 표정이 없는 모델이면")
        print("모델 설정의 핫키에서 동작을 'Play Animation'으로 하고 .motion3.json을 고른 핫키를 먼저 만들어 주세요.")
        client.close()
        return

    # 번호 -> 매핑 값(표정 파일 이름, 또는 "hotkey:이름")
    options = list(expressions) + [HOTKEY_PREFIX + name for name, _ in motions]
    motion_files = {HOTKEY_PREFIX + name: file_name for name, file_name in motions}

    def describe(value):
        if value is None:
            return "없음"
        if value.startswith(HOTKEY_PREFIX):
            return f"[모션] {value[len(HOTKEY_PREFIX):]}"
        return value

    print(f"\n현재 모델의 표정 파일 {len(expressions)}개, 모션 핫키 {len(motions)}개:")
    for number, value in enumerate(options, start=1):
        if value.startswith(HOTKEY_PREFIX):
            print(f"  {number:>3}. [모션] {value[len(HOTKEY_PREFIX):]}  ({motion_files[value]})")
        else:
            print(f"  {number:>3}. {value}")
    print("\n감정마다 쓸 번호를 입력하세요.")
    print("  숫자 = 사용 / 0 = 없음(기본 얼굴) / p숫자 = 미리보기 / 그냥 Enter = 현재 값 유지")
    print("  (어울리는 게 없는 감정은 0을 고르면 됩니다. 하나를 여러 감정에 써도 됩니다.)\n")

    valid = set(options)
    chosen = {}
    previewed = set()  # 미리보기로 켠 표정만 기억해서, 사용자가 직접 켜둔 표정은 건드리지 않는다
    try:
        for emotion, current in EXPRESSION_FILES.items():
            label = describe(current) if (current is None or current in valid) else f"{describe(current)} (이 모델에 없음)"
            while True:
                answer = input(f"[{emotion}] 현재: {label} > ").strip().lower()
                preview = answer.startswith("p")
                digits = answer[1:] if preview else answer

                if answer == "":
                    chosen[emotion] = current if current in valid else None
                    break
                if not digits.isdigit() or int(digits) > len(options):
                    print(f"  0~{len(options)} 사이의 번호를 입력하세요. (미리보기는 p3 처럼)")
                    continue

                target = options[int(digits) - 1] if int(digits) > 0 else None
                if preview:
                    if target and target.startswith(HOTKEY_PREFIX):
                        ok = client.trigger_hotkey(target[len(HOTKEY_PREFIX):])
                    else:
                        # 미리보기 전부터 켜져 있던 표정(사용자가 직접 켠 것)은 정리 대상에 넣지 않는다
                        already_on = bool(target) and dict(client.list_expressions()).get(target, False)
                        ok = client.apply_expression(target, set(previewed))
                        if ok and target and not already_on:
                            previewed.add(target)
                    print("  → 미리보기 적용" if ok else "  → 적용하지 못했습니다")
                    continue
                chosen[emotion] = target
                break
    except (EOFError, KeyboardInterrupt):
        print("\n중간에 멈춰서 저장하지 않았습니다.")
        client.apply_expression(None, previewed)
        client.close()
        return

    client.apply_expression(None, previewed)  # 미리보기로 켠 표정만 정리한다
    with open(path, "w", encoding="utf-8") as f:
        json.dump(chosen, f, ensure_ascii=False, indent=2)

    print(f"\n[저장 완료] {path}")
    for emotion, value in chosen.items():
        print(f"  {emotion:<4} → {describe(value) if value else '없음(기본 얼굴)'}")
    print("\n이제 vtuber_chat_test.py를 실행하면 이 연결이 자동으로 적용됩니다.")
    client.close()


if __name__ == "__main__":
    if "--map" in sys.argv:
        run_mapping()
    else:
        _interactive_test()