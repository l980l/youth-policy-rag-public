# HTML 웹 버전 무료 배포 준비

이 설정은 기존 HTML·CSS·JavaScript 화면과 Python 검색·AI API를 Vercel에 함께 올리기 위한 것입니다. Streamlit으로 화면을 바꾸지 않습니다. **아직 Vercel 실배포 검증 전이며, 설정 파일이 있다는 것만으로 공개 서비스가 운영되는 것은 아닙니다.**

## 무료 사용 조건

- 개인·비상업 프로젝트용 **Vercel Hobby**만 사용합니다. Pro 체험, 결제 수단 등록, 유료 부가 기능은 필요하지 않습니다.
- 이 프로젝트는 모델과 Python 의존성이 커서 **Python Large Functions 베타** 지원이 필요합니다. 프로젝트에서 지원되지 않으면 배포를 중단합니다. 유료 플랜으로 전환하지 않습니다.
- Gemini API 프로젝트가 **결제가 연결되지 않은 무료 등급**인지 Google AI Studio에서 확인합니다. 결제가 연결된 프로젝트의 지출을 앱에서 통제할 수는 없습니다.
- 무료 한도 소진, 제공자 정책 변경, 콜드 스타트 때문에 서비스가 제한되거나 느려질 수 있습니다. 상시 가동을 보장하지 않습니다.

공식 문서: [Hobby](https://vercel.com/docs/plans/hobby), [함수 제한](https://vercel.com/docs/functions/limitations), [Python 런타임](https://vercel.com/docs/functions/runtimes/python), [Gemini 요금](https://ai.google.dev/gemini-api/docs/pricing).

## 배포 설정

1. 공개 GitHub 저장소를 Vercel의 **Hobby 계정**에 연결합니다. Framework는 Other, Python은 3.12입니다. 빌드·라우팅은 저장소의 `vercel.json`을 사용합니다.
2. Vercel의 프로젝트 환경 변수에 아래 값을 등록합니다. 실제 키는 Git 파일이나 브라우저 코드에 넣지 않습니다.

| 변수 | 값 |
| --- | --- |
| `YOUTH_API_KEY` | 본인의 승인된 온통청년 API 키 |
| `LLM_API_KEY` | 무료 등급 Gemini 프로젝트의 API 키 |
| `LLM_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `LLM_MODEL` | `gemini-3.5-flash-lite` |
| `EMBEDDING_PROVIDER` | `local` |
| `FREE_TIER_CONFIRMED` | 처음에는 `false`. 결제 미연결 무료 등급을 직접 확인한 뒤 `true` |
| `VERCEL_SUPPORT_LARGE_FUNCTIONS` | `1` (프로젝트의 베타 지원 여부도 확인) |

`FREE_TIER_CONFIRMED`는 운영자의 확인 표시이며, 앱이 Google 결제 상태를 조회해 보증하는 기능은 아닙니다. 기본값에서는 호스팅 AI 호출을 차단하고 화면에 준비 중이라고 표시합니다. `GMS_KEY`는 등록하지 않습니다.

3. 빌드가 정책 목록을 수집하고 로컬 E5 모델로 색인을 생성합니다. `runtime_assets`는 빌드 산출물이며 Git에 올리지 않습니다. API 키·기존 질문·대화 캐시·개인 위키도 배포하지 않습니다.
4. 배포 로그에서 Large Functions 적용과 빌드 성공을 확인합니다. `/api/health`, `/api/bootstrap`, 목록·상세·AI 맞춤 탐색·정책 질의응답을 실제 배포 주소에서 확인해야 완료입니다. 메모리 초과·빌드 제한 발생 시 무료 배포 방식의 추가 조정이 필요합니다.

## 적용한 제한

- Gemini 허용 주소와 모델, 로컬 임베딩 설정을 확인한 뒤에만 호스팅 모델을 호출합니다.
- 자동 재시도를 끄고 모델 호출당 출력 한도를 4,096토큰으로 설정합니다.
- **런타임별** 모델 호출은 UTC 하루 60회, 최근 60초 12회로 제한합니다. 질문 하나가 여러 모델 호출을 사용할 수 있고 실패한 호출도 집계합니다.
- 이 제한은 임시 SQLite 저장소 기준입니다. 인스턴스가 여러 개 생기거나 저장소가 초기화되면 합산되지 않습니다. **계정 전체 한도나 과금 방지 장치가 아닙니다.** 추가 지출을 피하려면 제공자 무료 등급 자체가 유지되어야 합니다.
- 한 런타임에서 AI 작업을 동시에 하나만 처리하고 초과 요청에는 429를 반환합니다.
- 검색 데이터만 임시 저장소로 복사하고 모델은 번들에서 읽습니다. 대화는 브라우저 메모리에 보관하며 새로고침하면 초기화됩니다.

## 로컬에서 배포 자료 검증

기존 정책 데이터가 있으면 재수집 없이 별도로 정제한 배포 자료를 생성할 수 있습니다.

```bash
python -m youth_policy.prepare_deploy --source-data /정책/데이터/경로 --model-cache /고정리비전/모델/경로
python -m unittest discover -s tests -q
```

생성된 `runtime_assets/`는 `.gitignore`와 `.vercelignore`에서 제외됩니다. 기존 생성 폴더가 있으면 덮어쓰지 않고 중단하므로 내용을 확인한 뒤 생성 산출물만 정리하고 다시 실행합니다. 실제 배포에서는 빌드가 새로 생성합니다.

## 준비 단계 검증 기록 (2026-10-25)

- 기존 데이터에서 정책 3,498개·정책 자체 근거 16,640개 청크를 새 DB로 내보냈습니다. 기존 질문 메타데이터와 연관 검색 문서는 제외했습니다.
- 기존 DB에서 읽히지 않는 97개 청크 벡터는 해당 공개 정책 본문만 로컬 E5로 다시 임베딩했습니다. 원본 데이터는 복구 대상으로 수정하지 않았습니다.
- 정제된 검색 데이터는 약 160MB, 모델·토크나이저는 약 487MB입니다. 모두 Git 제외 산출물입니다.
- 번들 시드의 임시 저장소 복사 → 목록·상세 → 실제 E5/Chroma 검색 → 설정 미확인 AI 차단을 로컬에서 확인했습니다. 약 2.78초, 최대 메모리 약 1,179MiB였습니다. **macOS 로컬 측정이며 Vercel Linux 성능이나 메모리를 보장하지 않습니다.**
- 자동 테스트 74개, JavaScript 문법 검사, 설치된 130개 패키지 호환성 검사가 통과했습니다.
- Vercel CLI 로그인 정보가 없고 브라우저의 계정 페이지 접근 권한이 거부되어, 실제 배포·자산 번들 포함·외부 AI 응답은 아직 검증하지 못했습니다.
