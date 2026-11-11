# 영상 업스케일러

DVD를 비롯한 저화질 영상을 HD~4K로 업스케일하는 Windows용 프로그램입니다.
ffmpeg로 인터레이스 제거·노이즈 제거·확대·선명화를 하고, Real-ESRGAN이 있으면 AI 업스케일링도 할 수 있습니다.

## 기능

- **입력**: MP4, MKV, AVI, MOV, WMV, WebM, FLV, 3GP, VOB 등 ffmpeg가 읽는 영상, DVD 폴더(VIDEO_TS)
- **출력 해상도**: 720p, 1080p, 1440p, 4K, 원본의 2배
- **화질 보정**: 인터레이스(줄무늬) 제거, 노이즈 제거, 선명화, 화면비·색공간(BT.601 → BT.709) 보정
- **AI 업스케일링**: Real-ESRGAN (그래픽 카드 사용, 그래픽 카드 선택 가능, 중지 후 이어 하기)
- **대기열**: 여러 영상을 넣어 두면 하나씩 차례로 변환, 변환 중 PC 절전 방지, 대기열·설정 자동 저장
- **자막**: 영상 속 자막 트랙과 같은 이름의 자막 파일(srt, smi 등)을 함께 옮김
- **미리보기**: 한 장면을 뽑아 단순 확대와 업스케일 결과를 나란히 비교
- **저장 위치**: 지정한 폴더 또는 원본 영상과 같은 폴더

## 준비

1. **Python 3** (tkinter 포함)
2. **ffmpeg**

   ```
   winget install Gyan.FFmpeg
   ```

3. **Real-ESRGAN** (AI 업스케일링을 쓸 때만)
   [Real-ESRGAN 릴리스](https://github.com/xinntao/Real-ESRGAN/releases)에서
   `realesrgan-ncnn-vulkan-...-windows.zip`을 받아 `tools/realesrgan/` 폴더에 풉니다.
   (`tools/realesrgan/realesrgan-ncnn-vulkan.exe`와 `models` 폴더가 있어야 합니다.)

`tools` 폴더 안에 `ffmpeg.exe`를 넣어 두어도 프로그램이 찾아 씁니다.

## 실행

`run.bat`을 더블클릭하거나:

```
py dvd_upscaler.py
```

명령줄로도 쓸 수 있습니다.

```
py dvd_upscaler.py 영상1.mp4 영상2.avi --height 1080 --out-dir 결과폴더
py dvd_upscaler.py 영상.vob --ai --container mkv
py dvd_upscaler.py --help
```

## 참고

- 복사 방지(CSS)가 걸린 상업용 DVD는 읽을 수 없습니다.
- AI 업스케일링은 오래 걸립니다. 내장 그래픽(Radeon 8050S) 기준 DVD 해상도에서 초당 약 10프레임입니다.
- DVD·블루레이의 그림 형식 자막은 MKV로 저장할 때만 옮겨집니다.
- HDR 영상은 일반(SDR) 8비트로 저장됩니다.
