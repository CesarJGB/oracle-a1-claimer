# Changelog

## 2026-09-11

- Primera versión funcional del claimer para `VM.Standard.A1.Flex`.
- Soporte para región Monterrey, imagen ARM, subred pública y clave SSH.
- Consulta opcional de capacity report con fallback a intentos directos.
- Reintentos idempotentes para fallos transitorios y protección contra duplicados.
- Persistencia del OCID/IP y notificación opcional por Telegram.
- Servicio `systemd`, instalador de Ubuntu y pruebas locales.
