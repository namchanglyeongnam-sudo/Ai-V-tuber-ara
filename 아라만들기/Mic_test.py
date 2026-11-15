"""
마이크 음성 인식 시험 (mic_test.py)

목적: 아라에 붙이기 전에, 마이크로 말한 한국어가 글자로 잘 나오는지만 따로 확인한다.
이 파일은 아라 코드와 별개이며, 아라 파일을 건드리지 않는다.

준비 (PowerShell):
  python -m pip install SpeechRecognition pyaudio
  (pyaudio 설치가 오류로 끝나면 그 메시지를 알려주세요)

주의: 구글 웹 음성 인식을 쓰므로 인터넷이 필요합니다. 이어폰을 끼고 시험하세요.
"""

import time
import speech_recognition as sr


def main():
    recognizer = sr.Recognizer()

    print("[마이크 목록]")
    for index, name in enumerate(sr.Microphone.list_microphone_names()):
        print(f"  {index}: {name}")
    print("\n기본 마이크로 시험합니다. 다른 마이크를 쓰려면 위 번호를 Microphone(device_index=번호)에 넣으세요.\n")

    with sr.Microphone() as source:
        print("주변 소리를 1초 동안 듣는 중입니다. 조용히 해 주세요...")
        recognizer.adjust_for_ambient_noise(source, duration=1)

        while True:
            answer = input("\nEnter = 녹음 시작 / q = 종료: ").strip().lower()
            if answer == "q":
                break

            print("말씀하세요 (5초 안에 말을 시작하고, 최대 10초)...")
            try:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
            except sr.WaitTimeoutError:
                print("  → 소리가 들리지 않았습니다.")
                continue

            print("  인식 중...")
            started = time.perf_counter()
            try:
                text = recognizer.recognize_google(audio, language="ko-KR")
                print(f"  → 인식 결과: {text}")
            except sr.UnknownValueError:
                print("  → 알아듣지 못했습니다.")
            except sr.RequestError as error:
                print(f"  → 인식 서비스에 연결하지 못했습니다: {error}")
            print(f"  (인식에 걸린 시간 {time.perf_counter() - started:.2f}초)")


if __name__ == "__main__":
    main()