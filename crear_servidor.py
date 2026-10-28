"""Pide a Oracle Cloud el servidor gratis (Ampere A1) una y otra vez hasta que haya espacio.

Uso:  python crear_servidor.py  (en GitHub Actions: .github/workflows/buscar.yml)
Lee la conexión de ~/.oci/config (llave de API del dueño). La primera vez crea la red
(VCN + internet gateway + subred pública + puertos 22/80/443). Cuando consigue el
servidor, escribe su IP pública en infra/oracle/servidor.txt y termina.
"""
import datetime
import os
import pathlib
import sys
import time

import oci

REGION_CONFIG = oci.config.from_file()
TENANCY = REGION_CONFIG["tenancy"]
NOMBRE = "bot-ventas"
NOMBRE_RED = "red-bot-ventas"
# Primero el tamaño completo gratis; si no hay espacio, el mediano.
TAMANOS = [(4, 24), (2, 12)]
DISCO_GB = 100
ESPERA_SEG = 90
# En GitHub Actions cada corrida dura máximo 6 h: salir antes y dejar que la siguiente continúe.
MAX_MINUTOS = float(os.environ.get("MAX_MINUTOS", "0"))
LLAVE_SSH = pathlib.Path.home() / ".ssh" / "oracle_bot.pub"
AQUI = pathlib.Path(__file__).resolve().parent
LOG = AQUI / "reintentos.log"
RESULTADO = AQUI / "servidor.txt"

red = oci.core.VirtualNetworkClient(REGION_CONFIG)
computo = oci.core.ComputeClient(REGION_CONFIG)
identidad = oci.identity.IdentityClient(REGION_CONFIG)


def log(msg: str) -> None:
    linea = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(linea + "\n")


def esperar(cliente_get, estado):
    return oci.wait_until(red, cliente_get, "lifecycle_state", estado, max_wait_seconds=300).data


def asegurar_red() -> str:
    """Devuelve el id de la subred pública; la crea si no existe."""
    vcns = [v for v in red.list_vcns(TENANCY, display_name=NOMBRE_RED).data if v.lifecycle_state == "AVAILABLE"]
    if vcns:
        vcn = vcns[0]
    else:
        log("Creando la red (VCN)...")
        vcn = red.create_vcn(oci.core.models.CreateVcnDetails(
            compartment_id=TENANCY, display_name=NOMBRE_RED, cidr_blocks=["10.0.0.0/16"], dns_label="botventas")).data
        vcn = esperar(red.get_vcn(vcn.id), "AVAILABLE")

    subredes = [s for s in red.list_subnets(TENANCY, vcn_id=vcn.id).data if s.lifecycle_state == "AVAILABLE"]
    if subredes:
        return subredes[0].id

    log("Creando internet gateway, rutas y puertos...")
    igw = red.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=TENANCY, vcn_id=vcn.id, is_enabled=True, display_name="igw-bot-ventas")).data
    esperar(red.get_internet_gateway(igw.id), "AVAILABLE")
    red.update_route_table(vcn.default_route_table_id, oci.core.models.UpdateRouteTableDetails(route_rules=[
        oci.core.models.RouteRule(destination="0.0.0.0/0", destination_type="CIDR_BLOCK", network_entity_id=igw.id)]))

    lista = red.get_security_list(vcn.default_security_list_id).data
    reglas = list(lista.ingress_security_rules)
    for puerto in (80, 443):
        reglas.append(oci.core.models.IngressSecurityRule(
            protocol="6", source="0.0.0.0/0",
            tcp_options=oci.core.models.TcpOptions(destination_port_range=oci.core.models.PortRange(min=puerto, max=puerto))))
    red.update_security_list(lista.id, oci.core.models.UpdateSecurityListDetails(ingress_security_rules=reglas))

    sub = red.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=TENANCY, vcn_id=vcn.id, cidr_block="10.0.0.0/24", display_name="subred-publica",
        dns_label="publica", prohibit_public_ip_on_vnic=False)).data
    return esperar(red.get_subnet(sub.id), "AVAILABLE").id


def imagen_ubuntu() -> str:
    imgs = computo.list_images(TENANCY, operating_system="Canonical Ubuntu", operating_system_version="24.04",
                               shape="VM.Standard.A1.Flex", sort_by="TIMECREATED", sort_order="DESC").data
    return imgs[0].id


def ya_existe():
    for i in computo.list_instances(TENANCY, display_name=NOMBRE).data:
        if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
            return i
    return None


def ip_publica(instancia_id: str) -> str:
    for adj in computo.list_vnic_attachments(TENANCY, instance_id=instancia_id).data:
        vnic = red.get_vnic(adj.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return ""


def terminar(instancia) -> None:
    instancia = oci.wait_until(computo, computo.get_instance(instancia.id), "lifecycle_state", "RUNNING",
                               max_wait_seconds=900).data
    ip = ip_publica(instancia.id)
    RESULTADO.write_text(f"nombre={instancia.display_name}\nid={instancia.id}\nip={ip}\n"
                         f"ocpu={instancia.shape_config.ocpus}\nmemoria_gb={instancia.shape_config.memory_in_gbs}\n",
                         encoding="utf-8")
    log(f"LISTO: servidor {instancia.display_name} encendido, IP publica {ip} "
        f"({instancia.shape_config.ocpus} OCPU, {instancia.shape_config.memory_in_gbs} GB)")
    if os.name == "nt":
        os.system('msg * "Oracle: el servidor bot-ventas ya esta creado. Avisale a Claude."')


def main() -> None:
    existente = ya_existe()
    if existente:
        log("El servidor ya existe; no se crea otro.")
        terminar(existente)
        return

    # Una llave de API recién creada tarda unos minutos en valer para todos los servicios (401 intermitente).
    while True:
        try:
            ad = identidad.list_availability_domains(TENANCY).data[0].name
            subred = asegurar_red()
            imagen = imagen_ubuntu()
            break
        except oci.exceptions.ServiceError as e:
            if e.status != 401:
                raise
            log("Oracle todavia no acepta la llave nueva; espero 60 s.")
            time.sleep(60)
        except Exception as e:
            log(f"Fallo de conexion preparando la red ({type(e).__name__}); espero 60 s.")
            time.sleep(60)
    llave = LLAVE_SSH.read_text().strip()
    intento = 0
    inicio = time.monotonic()
    log(f"Empezando reintentos en {ad} (cada {ESPERA_SEG} s). Cierra esta ventana para detener.")

    while True:
        if MAX_MINUTOS and time.monotonic() - inicio > MAX_MINUTOS * 60:
            log(f"Pasaron {MAX_MINUTOS:.0f} min sin espacio; la siguiente corrida sigue intentando.")
            return
        for ocpu, mem in TAMANOS:
            intento += 1
            try:
                inst = computo.launch_instance(oci.core.models.LaunchInstanceDetails(
                    compartment_id=TENANCY, availability_domain=ad, display_name=NOMBRE,
                    shape="VM.Standard.A1.Flex",
                    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpu, memory_in_gbs=mem),
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        image_id=imagen, boot_volume_size_in_gbs=DISCO_GB),
                    create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subred, assign_public_ip=True),
                    metadata={"ssh_authorized_keys": llave})).data
                log(f"Intento {intento}: CONSEGUIDO con {ocpu} OCPU / {mem} GB. Esperando que encienda...")
                terminar(inst)
                return
            except oci.exceptions.ServiceError as e:
                if "capacity" in (e.message or "").lower():
                    log(f"Intento {intento}: sin espacio para {ocpu} OCPU / {mem} GB.")
                elif e.status == 429:
                    log(f"Intento {intento}: Oracle pide ir mas lento; espero el doble.")
                    time.sleep(ESPERA_SEG)
                else:
                    log(f"Intento {intento}: error {e.status} {e.code}: {e.message}")
                    if e.status in (400, 404):
                        sys.exit(1)
            except Exception as e:  # cortes de internet del hotspot: seguir intentando
                log(f"Intento {intento}: fallo de conexion ({type(e).__name__}); sigo.")
            time.sleep(ESPERA_SEG)


if __name__ == "__main__":
    main()
