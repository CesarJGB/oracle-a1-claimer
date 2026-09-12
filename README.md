# OCI Always Free A1 claimer

Automatiza la comprobación y el intento de creación de una instancia
`VM.Standard.A1.Flex` en Oracle Cloud Infrastructure. Está preparado para
Monterrey con los valores que indicaste: **2 OCPU y 12 GB de RAM**.

Usa la API oficial de OCI mediante una clave de firma. No automatiza el
navegador, no intenta saltarse CAPTCHA ni crea cuentas. Cuando OCI devuelve
`Out of host capacity`, espera y vuelve a intentar con una frecuencia moderada.

## Qué hace

1. Carga el perfil de OCI desde `~/.oci/config`.
2. Comprueba los availability domains de la región.
3. Usa una imagen ARM indicada por OCID o detecta la imagen Oracle Linux A1 más reciente.
4. Usa una subred pública indicada por OCID o intenta detectar una única subred pública.
5. Consulta el capacity report cuando la API está disponible.
6. Intenta crear una sola instancia con el nombre configurado y una clave SSH pública.
7. Usa un token de idempotencia durante reintentos de red y comprueba el nombre antes de volver a crear.
8. Guarda el OCID/IP en un archivo local y puede avisar por Telegram.
9. Puede enviar un heartbeat horario con el estado y las métricas de la última hora.

El capacity report no reserva capacidad: la disponibilidad puede cambiar entre
la consulta y la creación. Por eso el script también realiza un intento real de
creación cuando corresponde.

## Requisitos

- Una cuenta OCI con la región **Mexico Northeast (Monterrey)** suscrita.
- Un host encendido permanentemente. Un VPS Ubuntu es ideal.
- Python 3.9 o posterior y `python3-venv`.
- Una clave API de OCI.
- Una clave pública SSH para instalarla en la instancia.
- Un compartimento y una subred pública existentes.

Monterrey usa el identificador `mx-monterrey-1` y tiene un availability domain.
El nombre exacto de ese dominio es específico de cada tenancy; el script lo
obtiene automáticamente.

## 1. Crear la clave API de OCI

La clave API de OCI y la clave SSH son cosas distintas. La primera autoriza al
script a llamar la API; la segunda permite entrar a la VM.

En el host que ejecutará el script:

```bash
mkdir -p ~/.oci
openssl genrsa -out ~/.oci/oci_api_key.pem 2048
chmod 600 ~/.oci/oci_api_key.pem
openssl rsa -pubout -in ~/.oci/oci_api_key.pem -out ~/.oci/oci_api_key_public.pem
```

En OCI abre **Profile → User settings → API keys → Add API key**, sube
`~/.oci/oci_api_key_public.pem` y copia el fragmento de configuración que
Oracle muestra. Debe quedar aproximadamente así, con tus valores reales:

```ini
[DEFAULT]
user=ocid1.user.oc1....
fingerprint=aa:bb:cc:dd:...
tenancy=ocid1.tenancy.oc1....
region=mx-monterrey-1
key_file=/home/USUARIO/.oci/oci_api_key.pem
```

Guárdalo en `~/.oci/config` y protege la carpeta:

```bash
chmod 700 ~/.oci
chmod 600 ~/.oci/config ~/.oci/oci_api_key.pem
```

No compartas ni subas `oci_api_key.pem`, `~/.oci/config` ni el archivo `.env`.

## 2. Preparar el proyecto

```bash
cd oracle-a1-claimer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edita `.env` y completa al menos:

```ini
OCI_COMPARTMENT_ID=ocid1.compartment.oc1....
OCI_SUBNET_ID=ocid1.subnet.oc1....
OCI_SSH_PUBLIC_KEY_PATH=/home/USUARIO/.ssh/id_ed25519.pub
```

Conviene indicar `OCI_SUBNET_ID` manualmente. Debe ser una subred pública de la
misma región/VCN; de lo contrario no podrá asignarse la IP pública. Si omites
ese valor, el script solo elegirá automáticamente una subred cuando no haya
ambigüedad.

Si no conoces el OCID de la imagen, deja `OCI_IMAGE_ID` vacío. El script
buscará la imagen Oracle Linux compatible con A1. Si quieres fijar una versión
concreta, ejecuta primero:

```bash
set -a; . ./.env; set +a
python oci_a1_claimer.py --discover
```

## 3. Validar sin crear nada

Este comando comprueba la clave API, el compartimento, la región, la imagen, la
subred y la clave SSH. **No intenta crear una VM.**

```bash
set -a; . ./.env; set +a
python oci_a1_claimer.py --validate-only
```

Si devuelve `Validación terminada correctamente`, puedes hacer un único ciclo:

```bash
python oci_a1_claimer.py --once
```

`--once` sí puede crear la instancia si hay capacidad. Para dejarlo revisando
continuamente, ejecuta:

```bash
python oci_a1_claimer.py
```

El intervalo predeterminado es de 60 segundos con una variación aleatoria de
hasta 15 segundos. No lo reduzcas a unos pocos segundos: no aumenta de forma
confiable la capacidad y puede provocar respuestas de límite de solicitudes.

## 4. Notificación opcional por Telegram

1. Habla con `@BotFather` en Telegram y usa `/newbot`.
2. Envía `/start` al bot creado.
3. Obtén el `chat_id` consultando `getUpdates`:

```bash
read -r TELEGRAM_BOT_TOKEN
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```

Busca el valor numérico de `message.chat.id` y completa en `.env`:

```ini
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

El bot no es necesario para reclamar la instancia. Se utiliza para avisar
cuando la creación termina y para el heartbeat horario instalado con el
servicio. El heartbeat muestra si el claimer sigue activo y cuenta en los logs
de la última hora:

- comprobaciones de capacidad;
- intentos directos de creación;
- intentos rechazados por falta de capacidad;
- respuestas `HTTP 429` de rate limit.

El heartbeat lee de `claimer.env` únicamente `TELEGRAM_BOT_TOKEN` y
`TELEGRAM_CHAT_ID`; no ejecuta ni carga el archivo completo como código shell.
Así, valores con espacios como `OCI_IMAGE_OS=Canonical Ubuntu` no provocan el
error `Ubuntu: command not found`.

Cuando existe `/var/lib/oci-a1-claimer/instance.json`, el heartbeat deja de
enviar mensajes porque la instancia ya fue creada y el claimer envía su propia
notificación final.

## 5. Dejarlo como servicio en Ubuntu

Desde el directorio del proyecto, con tu usuario normal:

```bash
chmod +x install.sh oci_a1_claimer.py
./install.sh
sudo nano /etc/oci-a1-claimer/claimer.env
```

En `claimer.env` pega los valores de `.env`, preferiblemente con rutas
absolutas para `OCI_CONFIG_FILE` y `OCI_SSH_PUBLIC_KEY_PATH`. Después:

```bash
sudo chmod 600 /etc/oci-a1-claimer/claimer.env
sudo systemctl enable --now oci-a1-claimer
sudo systemctl enable --now oci-a1-heartbeat.timer
sudo systemctl status oci-a1-claimer
systemctl list-timers oci-a1-heartbeat.timer --no-pager
sudo journalctl -u oci-a1-claimer -f
```

Puedes comprobar el mensaje sin esperar a la siguiente hora:

```bash
sudo systemctl start oci-a1-heartbeat.service
sudo journalctl -u oci-a1-heartbeat.service -n 20 --no-pager
```

Cuando la creación sea exitosa, el servicio detectará la instancia por su
nombre y terminará. Los datos quedan en:

```text
/var/lib/oci-a1-claimer/instance.json
```

Para detenerlo manualmente:

```bash
sudo systemctl disable --now oci-a1-claimer
sudo systemctl disable --now oci-a1-heartbeat.timer
```

## Errores habituales

- **`NotAuthorizedOrNotFound`**: la clave API no tiene permisos sobre el
  compartimento o alguno de los OCID no corresponde a la región.
- **`Out of host capacity`**: no hay capacidad física disponible en ese
  momento. El script lo registra y continúa después.
- **`LimitExceeded` o error de cuota**: la cuenta ya usa parte de la cuota
  Always Free. Revisa las instancias, volúmenes e IP públicas existentes.
- **No se encuentra imagen**: define manualmente `OCI_IMAGE_ID` con una imagen
  ARM compatible o revisa `OCI_IMAGE_OS_VERSION`.
- **Subred privada**: define una subred pública o cambia
  `OCI_ASSIGN_PUBLIC_IP=false` si realmente quieres una VM sin IP pública.
- **`capacity report` no autorizado**: no es fatal; el programa desactiva esa
  consulta y continúa con intentos directos controlados.

## Permisos para un usuario dedicado

Si no usarás el usuario administrador, crea un usuario/grupo de automatización
con permisos limitados en IAM. Como punto de partida, el grupo necesita poder
inspeccionar availability domains e imágenes, administrar la familia de
instancias en el compartimento elegido y usar la familia de red de ese
compartimento. Verifica la sintaxis exacta y ajusta el alcance en la referencia
de políticas de OCI antes de aplicarla.

## Referencias oficiales

- [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/)
- [Regions and Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm)
- [Required Keys and OCIDs](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)
- [Creating an Instance](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)
- [OCI Python SDK](https://docs.oracle.com/en-us/iaas/tools/python/latest/)
