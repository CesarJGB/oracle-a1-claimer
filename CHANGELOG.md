# Changelog

## 2026-09-12

- Se agregó el heartbeat horario de Telegram con métricas de la última hora.
- Se añadieron su servicio y timer de `systemd`, con ejecución persistente cada hora.
- El heartbeat alerta si el claimer está detenido y se omite después de una creación exitosa.
- La lectura aislada de las variables de Telegram evita ejecutar `claimer.env` como shell.
- El instalador y el README ahora incluyen la instalación y verificación del heartbeat.

## 2026-09-11

- Primera versión funcional del claimer para `VM.Standard.A1.Flex`.
- Soporte para región Monterrey, imagen ARM, subred pública y clave SSH.
- Consulta opcional de capacity report con fallback a intentos directos.
- Reintentos idempotentes para fallos transitorios y protección contra duplicados.
- Persistencia del OCID/IP y notificación opcional por Telegram.
- Servicio `systemd`, instalador de Ubuntu y pruebas locales.
