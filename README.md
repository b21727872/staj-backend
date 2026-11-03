# Proje Portalı API

FastAPI servisi. Bölüm içeriklerini verir, yönetim girişini doğrular ve iletişim formunu Resend ile gönderir.

## Çalıştırma

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8742}
```

Arayüz `http://127.0.0.1:43127` adresinden `/api` isteklerini bu porta iletir.

Deneme hesabı: `admin` / `proje2026`

## Ortam değişkenleri

| Değişken | Varsayılan |
| --- | --- |
| `ADMIN_USER` | `admin` |
| `ADMIN_PASSWORD` | `proje2026` |
| `AUTH_SECRET` | `dev-secret-change-me` |
| `RESEND_API_KEY` | boş |
| `CONTACT_TO_EMAIL` | boş |
| `CONTACT_FROM_EMAIL` | `Proje Portalı <onboarding@resend.dev>` |
| `CORS_ORIGINS` | yerel arayüz adresleri |

Production’da arayüz başka bir adresteyse onu virgülle ekleyin. Sonda `/` olmasın.

```bash
CORS_ORIGINS=https://staj-frontend.up.railway.app
```

`RESEND_API_KEY` veya `CONTACT_TO_EMAIL` yoksa iletişim mesajı `data/inbox.json` dosyasına yazılır ve e-posta gönderilmez.
