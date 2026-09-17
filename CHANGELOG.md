# Changelog

## 2026-09-16

- **Fault domain automático en fallback directo**: Se añadió `OCI_DIRECT_FAULT_DOMAIN_MODE`
  con valor predeterminado `auto` para omitir `fault_domain` en el request y permitir
  al scheduler de OCI ubicar la instancia libremente; modo `rotate` conserva la rotación
  secuencial persistida. Los reportes de capacidad conservan su fault domain explícito.
- **Comprobación de instancia basada en frescura**: Se eliminó la llamada obligatoria
  a `list_instances()` inmediatamente antes de cada launch. Se comprueba al arrancar,
  periódicamente (`OCI_EXISTING_CHECK_INTERVAL_SECONDS`), tras fallos ambiguos y si la
  frescura ha expirado. Las alertas de capacidad disponible pueden lanzar inmediatamente
  sin lecturas intermedias que arriesguen 429.
- **TTL, orden y deduplicación de candidatos de capacidad**: Los candidatos del
  capacity report se persisten como hints temporales con TTL (`OCI_CAPACITY_CANDIDATE_TTL_SECONDS=180`),
  se ordenan por frescura (los más recientes primero) y se deduplican actualizando
  marcas de tiempo. Hints antiguos sin fecha se descartan de forma segura.
  `OutOfHostCapacity` descarta el hint sin reencolarlo automáticamente.
- **Ritmo directo adaptativo y conservador**: Con `OCI_ADAPTIVE_DIRECT_INTERVAL=true`,
  el intervalo entre intentos directos se reduce gradualmente (-15 s por respuesta sin 429)
  dentro de los límites configurados (`OCI_ADAPTIVE_MIN_INTERVAL_SECONDS=120` y
  `OCI_ADAPTIVE_MAX_INTERVAL_SECONDS=600`) y se incrementa significativamente ante HTTP 429,
  respetando `Retry-After` y persistiendo el valor en `runtime.json`.
- **Heartbeat mejorado**: Muestra el intervalo directo actual y el número de candidatos
  de capacidad frescos en la notificación horaria si están disponibles.

## 2026-09-12

- Se agregó el heartbeat horario de Telegram con métricas de la última hora.
- Se añadieron su servicio y timer de `systemd`, con ejecución persistente cada hora.
- El heartbeat alerta si el claimer está detenido y se omite después de una creación exitosa.
- La lectura aislada de las variables de Telegram evita ejecutar `claimer.env` como shell.
- El instalador y el README ahora incluyen la instalación y verificación del heartbeat.
- Se eliminaron las ráfagas del fallback: hay como máximo una llamada real por
  ciclo y la rotación de fault domains queda persistida.
- Se separaron las revisiones del capacity report de las solicitudes reales,
  con un ritmo directo predeterminado de unas 15 solicitudes por hora cuando
  no hay capacidad reportada.
- Los 429 activan cooldown global con `Retry-After` o backoff exponencial con
  jitter; los timeouts y 5xx conservan el token de idempotencia al reintentar
  el mismo candidato en otro ciclo.
- Los fallos temporales del capacity report se reintentan y solo las
  limitaciones permanentes del endpoint activan el fallback durante la
  ejecución.
- Se añadió `runtime.json`, con escritura atómica, eventos de 24 horas,
  ventana exacta para métricas y recuperación segura ante ausencia o daño.
- El heartbeat muestra duración activa, hora local de Monterrey, porcentaje de
  rate limit sobre solicitudes reales y el próximo intento permitido.

## 2026-09-11

- Primera versión funcional del claimer para `VM.Standard.A1.Flex`.
- Soporte para región Monterrey, imagen ARM, subred pública y clave SSH.
- Consulta opcional de capacity report con fallback a intentos directos.
- Reintentos idempotentes para fallos transitorios y protección contra duplicados.
- Persistencia del OCID/IP y notificación opcional por Telegram.
- Servicio `systemd`, instalador de Ubuntu y pruebas locales.
