# buscador-oracle

Pide a Oracle Cloud (Always Free, Ampere A1) un servidor cada 90 s hasta que haya espacio.
Corre en GitHub Actions cada hora. Las llaves de Oracle están en los secretos del repositorio
(`OCI_CONFIG`, `OCI_KEY`, `SSH_PUB`), nunca en el código. Cuando consigue el servidor abre un
issue con la IP (llega aviso por correo) y se desactiva solo.
