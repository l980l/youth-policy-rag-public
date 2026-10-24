# 공개 배포본 검증

검증일: 2026-10-25. 새 공개용 폴더에 실제 .env나 정책 DB를 복사하지 않은 상태에서 확인했다.

| 검사 | 결과 |
| --- | --- |
| Python 자동 검사 | 52개 통과, 4.575초 |
| Python compileall | 통과 |
| JavaScript 문법 | 통과 |
| 설치 패키지 호환성 | 130개 확인, 충돌 없음 |
| .env.example 키 | 3개 키 값 모두 공란 |
| 키 패턴·개인 절대 경로 | 선택한 파일에서 미발견 |
| 교육 PDF·개인 위키·정책 DB | 미포함 |
| 이미지 | 자체 UI 화면 3장만 포함 |

자동 검사는 모델/API를 모의 대체하는 검사, 로컬 HTTP·색인 동작 등을 포함한다. 공개 배포본에서 실제 Gemini·온통청년 API로 새 질의를 실행한 검증은 아니며, 새 컴퓨터의 패키지 전체 설치도 다시 수행하지 않았다. 키 패턴 점검은 완전한 비밀 탐지 보증이 아니다.

화면은 2026-10-24에 촬영한 홈·이용 가이드·모바일 홈이다. 정책 원문과 개인 질문·모델 대화 화면은 제외했다. 홈의 정책 수는 촬영 시점의 상태이며 정책 데이터를 배포한다는 뜻이 아니다.

명령과 소스 해시는 [verification.json](verification.json)에 기록했다. 재현은 저장소 루트에서 다음 명령을 사용한다.

    python -m unittest discover -s tests -v
    python -m compileall -q app.py youth_policy tests
    node --check web/app.js

검증 도구가 필요하면 requirements-verification.txt를 별도로 설치한다. 본인 키와 데이터로 실제 서비스를 확인할 때에는 각 제공자의 API 한도를 사용한다.
