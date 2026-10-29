#!/usr/bin/env bash
# Instalación guiada en un VPS con Docker + Traefik.
# Uso: sudo ./install.sh
#      sudo ./install.sh --force   (regenera .env; rota SECRET_KEY)
#      sudo DOMAIN=... KOMMO_SUBDOMAIN=... ./install.sh --non-interactive
set -euo pipefail

cd "$(dirname "$0")"

FORCE=false
NON_INTERACTIVE=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    --non-interactive) NON_INTERACTIVE=true ;;
    -h|--help)
      sed -n '2,5p' "$0"
      exit 0
      ;;
    *)
      echo "Opción desconocida: $arg"
      exit 1
      ;;
  esac
done

if [ -f .env ] && [ "$FORCE" != true ]; then
  echo "Ya existe .env. Para conservarlo, ejecuta:"
  echo "  docker compose up -d --build"
  echo "Para regenerarlo (rota secretos), ejecuta:"
  echo "  ./install.sh --force"
  exit 1
fi

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Falta '$1'. Instálalo antes de continuar."
    exit 1
  fi
}

need docker
need openssl
if ! docker compose version >/dev/null 2>&1; then
  echo "Falta Docker Compose v2 (comando 'docker compose')."
  exit 1
fi

ask() {
  local prompt="$1" default="$2" var="${3:-}" value
  if [ -n "$var" ] && [ -n "${!var:-}" ]; then
    printf '%s' "${!var}"
    return
  fi
  if [ "$NON_INTERACTIVE" = true ]; then
    printf '%s' "$default"
    return
  fi
  read -r -p "$prompt [$default]: " value
  printf '%s' "${value:-$default}"
}

if [ "$NON_INTERACTIVE" = true ]; then
  DOMAIN="$(ask 'Dominio público (ej. track.tudominio.com)' '' DOMAIN)"
else
  DOMAIN="$(ask 'Dominio público (ej. track.tudominio.com)' 'track.tudominio.com' DOMAIN)"
fi
DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN%/}"
if [ -z "$DOMAIN" ] || [[ "$DOMAIN" != *.* ]]; then
  echo "Dominio inválido: $DOMAIN"
  exit 1
fi

KOMMO_SUBDOMAIN="$(ask 'Subdominio de Kommo (solo el nombre)' 'magiapastelerta4' KOMMO_SUBDOMAIN)"
TRAEFIK_NETWORK="$(ask 'Red de Traefik' 'traefik-proxy' TRAEFIK_NETWORK)"
TRAEFIK_CERTRESOLVER="$(ask 'Resolver TLS de Traefik' 'letsencrypt' TRAEFIK_CERTRESOLVER)"
TRAEFIK_ENTRYPOINT="$(ask 'Entrypoint HTTPS de Traefik' 'websecure' TRAEFIK_ENTRYPOINT)"

if ! docker network inspect "$TRAEFIK_NETWORK" >/dev/null 2>&1; then
  echo "La red '$TRAEFIK_NETWORK' no existe. Redes disponibles:"
  docker network ls
  echo "Ajusta el nombre y vuelve a ejecutar."
  exit 1
fi

PG_PASS="$(openssl rand -hex 24)"
DASH_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
SECRET_KEY="$(openssl rand -hex 32)"
WEBHOOK_SECRET="$(openssl rand -hex 32)"

if [ "$FORCE" = true ] && [ -f .env ]; then
  cp -a .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  echo "Backup del .env anterior creado."
fi

cat > .env <<EOF
DOMAIN=$DOMAIN
PUBLIC_BASE_URL=https://$DOMAIN

TRAEFIK_NETWORK=$TRAEFIK_NETWORK
TRAEFIK_CERTRESOLVER=$TRAEFIK_CERTRESOLVER
TRAEFIK_ENTRYPOINT=$TRAEFIK_ENTRYPOINT

SECRET_KEY=$SECRET_KEY
SECRET_KEY_FILE=/app/data/secret.key

POSTGRES_DB=attribution
POSTGRES_USER=attribution
POSTGRES_PASSWORD=$PG_PASS
DATABASE_URL=postgresql://attribution:$PG_PASS@db:5432/attribution

KOMMO_SUBDOMAIN=$KOMMO_SUBDOMAIN
KOMMO_TOKEN=
KOMMO_PIPELINE_ID=
KOMMO_WEBHOOK_SECRET=$WEBHOOK_SECRET
KOMMO_FIELD_TTCLID=
KOMMO_FIELD_REF=
KOMMO_FIELD_VALUE=
KOMMO_FIELD_CURRENCY=
KOMMO_FIELD_CONSENT=
KOMMO_STAGE_MAP={}

TIKTOK_EVENT_SOURCE=web
TIKTOK_PIXEL_ID=
TIKTOK_EVENT_SET_ID=
TIKTOK_ACCESS_TOKEN=
TIKTOK_TEST_EVENT_CODE=
TIKTOK_CURRENCY=COP
TIKTOK_DEFAULT_VALUE=0
TIKTOK_WEB_PAGE_URL=

WHATSAPP_NUMBER=
WHATSAPP_MESSAGE_TEMPLATE=Hola, quiero información. {ref}
DEFAULT_COUNTRY_CODE=57

DASHBOARD_USER=admin
DASHBOARD_PASSWORD=$DASH_PASS
REQUIRE_WEBHOOK_SECRET=true
STORE_RAW_PAYLOADS=true

WORKER_ENABLED=true
WORKER_POLL_SECONDS=1
LEAD_JOB_DELAY_SECONDS=5
MAX_ATTEMPTS=8
LOG_LEVEL=INFO
EOF

chmod 600 .env

echo
echo "Levantando contenedores..."
docker compose up -d --build
docker compose ps

cat <<EOF

============================================================
 Listo. Abre el panel:
   https://$DOMAIN

 Usuario: admin
 Clave:   $DASH_PASS

 Guarda esa clave. No se volverá a mostrar.
 En el panel entra a "Configuración" y pega ahí:
   - Token de Kommo
   - Access token y Pixel/Event Set ID de TikTok
   - Número de WhatsApp
   - Mapa de etapas

 La URL del webhook de Kommo aparece en el panel.
============================================================

Logs: docker compose logs -f app
EOF
