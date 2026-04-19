# docs.world.org Korean Mirror (자동 번역)

`https://docs.world.org/` 문서를 매일 1회 자동 수집하여, **코드 블록/식별자/명령어를 보존**하면서 한국어로 번역한 정적 사이트를 생성합니다.

## 핵심 동작

- 사이트맵(`sitemap.xml`) 기반 URL 수집
- 문서 본문 텍스트만 번역 (`main/article/theme-doc-markdown` 우선)
- `code`, `pre`, `kbd`, `samp`, `script`, `style` 태그 번역 제외
- `header/nav/footer/aside` 등 UI 컨테이너는 기본 제외
- 번역 캐시(문장+모델 해시 키)로 중복 비용 절감
- 내부 링크를 로컬 경로로 보정하여 미러 사이트에서 자연스럽게 이동
- `output/` 정적 HTML + `summary.json` 생성
- 사이트맵 기준 소스 fingerprint를 저장해 변경이 없으면 번역을 건너뜀(PASS)

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OPENAI_API_KEY="<YOUR_KEY>"
python scripts/translate_site.py \
  --base-url https://docs.world.org \
  --sitemap-url https://docs.world.org/sitemap.xml \
  --max-urls 500
```

주요 옵션:

- `--openai-model` (기본: `gpt-4.1-mini`)
- `--openai-base-url` (기본: `https://api.openai.com/v1`)
- `--openai-max-retries` (기본: `4`)
- `--request-timeout` (기본: `30`)
- `--state-path` (기본: `.state/source-fingerprint.json`)
- `--progress-state-path` (기본: `.state/translated-pages.json`)
- `--translate-sleep` (기본: `0.03`, 과금/429 완화를 위해 증가 가능)
- `--max-pages-per-run` / `--max-segments-per-run` / `--max-runtime-seconds` 실행 예산 제어
- `--max-batch-items` 미번역 세그먼트 배치 번역 크기
- `--priority-prefixes` 우선 번역 경로 prefix 지정

출력:

- `output/index.html`
- `output/**/index.html`
- `output/summary.json`
- `.translation-cache.json`

`summary.json`에는 실행 진단용 메타데이터도 포함됩니다:
- `summary_version`
- `rate_limit_count`
- `abort_reason`
- `elapsed_seconds`
- `cache_hit_ratio`
- `stats.api_calls_total`
- 구조화된 `errors` 항목(`url`, `error_type`, `status_code`, `message`)
- `pending_count`, `translated_count`, `deferred_count`

## 자동 실행 (GitHub Actions)

1. Repository Secrets에 `OPENAI_API_KEY` 추가
2. Actions 활성화
3. Pages 소스를 `GitHub Actions`로 설정

워크플로우:

- `.github/workflows/daily-translate.yml`
- 스케줄: `0 2 * * *` (UTC) = 매일 11:00 KST

## 테스트

```bash
python -m py_compile scripts/translate_site.py
python -m unittest discover -s tests -p 'test_*.py'
```


## 생성 결과가 안 보일 때 점검

- `output/`은 실행 시점에 생성되는 산출물이며 `.gitignore`에 포함되어 Git에 커밋되지 않습니다.
- 먼저 `OPENAI_API_KEY`가 설정되어 있는지 확인하세요.
- 사이트맵 접근 실패 시에도 이제 `summary.json`에 `errors`가 기록되고, 최소한의 `output/index.html`과 `output/summary.json`이 생성됩니다.
- GitHub Actions에서는 실행 로그와 아티팩트(`output`)를 Actions 탭에서 확인할 수 있습니다. 변경이 없으면 배포 단계는 PASS됩니다.
- 429가 반복되면 런타임에서 자동으로 대기 시간을 늘리고, 연속 429가 임계치에 도달하면 해당 실행은 중단(ABORT)하여 빈번한 실패 로그 폭증을 방지합니다.
- 429로 인해 실제 번역 성공(`urls_ok`)이 0건이면 `skip_reason=rate_limited`로 처리되어 배포를 건너뛰고 기존 Pages를 유지합니다.
- 진행 상태는 `.state/translated-pages.json`에 저장되어 다음 실행에서 deferred URL을 우선 재시도합니다.
