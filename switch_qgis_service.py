#!/usr/bin/env python3
"""Bascule les projets QGIS d'un nom de service a un autre, dans les deux sens.

Sert a faire basculer une selection de projets vers l'instance de test pendant
la migration a blanc, puis a les ramener a leur etat initial :

    aller  : --from prod --to prod_test
    retour : --from prod_test --to prod

Ne touche qu'au parametre service= des couches PostgreSQL/PostGIS. Une couche
qui porte deja le service cible est laissee telle quelle, donc relancer le
script deux fois de suite ne produit rien de plus.

Perimetre : soit un dossier entier (argument `racine`), soit une liste de
projets (--liste), un chemin par ligne, ce qui est le mode a utiliser pour un
panel de test choisi projet par projet.

Usage :
    python switch_qgis_service.py <racine> --from prod --to prod_test [--apply]
    python switch_qgis_service.py --liste panel.txt --from prod --to prod_test [--apply]

Le mode par defaut est une simulation : rien n'est ecrit sur le disque.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import re
import sys
import zipfile
from pathlib import Path

try:
    from lxml import etree
except ImportError:
    sys.exit("Ce script necessite lxml (pip install lxml).")

PG_PROVIDERS = {"postgres", "postgresraster"}
BACKUP_DIRNAME = "_backup_switch_service"

LOG_FIELDS = ["chemin_projet", "id_couche", "nom_couche", "statut", "service_avant", "service_apres"]


def service_actuel(datasource: str) -> str | None:
    m = re.search(r"service=(?:'((?:[^']|'')*)'|(\S+))", datasource)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").replace("''", "'") or None


def remplacer_service(datasource: str, cible: str) -> str:
    return re.sub(
        r"service=(?:'(?:[^']|'')*'|\S+)",
        f"service='{cible}'",
        datasource,
        count=1,
    )


def process_xml(xml_bytes: bytes, src: str, dst: str, chemin_log: str, rows: list[dict]):
    parser = etree.XMLParser(remove_comments=False, remove_pis=False, strip_cdata=False)
    tree = etree.fromstring(xml_bytes, parser=parser).getroottree()

    nb = 0
    for maplayer in tree.getroot().iter("maplayer"):
        provider_el = maplayer.find("provider")
        if provider_el is None or (provider_el.text or "") not in PG_PROVIDERS:
            continue
        ds_el = maplayer.find("datasource")
        if ds_el is None or not (ds_el.text or "").strip():
            continue
        layername_el = maplayer.find("layername")
        nom = (layername_el.text if layername_el is not None else None) or ""
        id_el = maplayer.find("id")
        id_couche = (id_el.text if id_el is not None else None) or ""

        actuel = service_actuel(ds_el.text)
        if actuel == src:
            statut, apres = "bascule", dst
        elif actuel == dst:
            statut, apres = "deja_sur_cible", dst
        elif actuel is None:
            statut, apres = "sans_service", ""
        else:
            statut, apres = "autre_service", actuel

        rows.append({
            "chemin_projet": chemin_log,
            "id_couche": id_couche,
            "nom_couche": nom,
            "statut": statut,
            "service_avant": actuel or "",
            "service_apres": apres,
        })

        if statut == "bascule":
            ds_el.text = remplacer_service(ds_el.text, dst)
            nb += 1

    if nb == 0:
        return None, 0
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=False), nb


def process_qgz(path: Path, src: str, dst: str, rows: list[dict]):
    with zipfile.ZipFile(path) as zin:
        infos = zin.infolist()
        noms = [i.filename for i in infos if i.filename.lower().endswith(".qgs")]
        if not noms:
            return None, 0
        contents = {i.filename: (i, zin.read(i.filename)) for i in infos}

    new_bytes, nb = process_xml(contents[noms[0]][1], src, dst, f"{path}!{noms[0]}", rows)
    if nb == 0:
        return None, 0

    contents[noms[0]] = (contents[noms[0]][0], new_bytes)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        for info, data in contents.values():
            ni = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
            ni.compress_type = info.compress_type
            ni.external_attr = info.external_attr
            zout.writestr(ni, data)
    return buf.getvalue(), nb


def nom_sauvegarde(path: Path) -> str:
    """Aplatit le chemin complet en un nom de fichier unique et lisible.
    Deux projets homonymes ranges dans des dossiers differents ne doivent pas
    ecraser mutuellement leur sauvegarde."""
    plat = str(path.resolve()).replace(":", "").replace("\\", "__").replace("/", "__")
    return plat.lstrip("_")


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp_switch")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def collecter_projets(args) -> list[Path]:
    if args.liste:
        chemins = []
        for ligne in args.liste.read_text(encoding="utf-8-sig").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#"):
                continue
            p = Path(ligne)
            if not p.exists():
                print(f"  [!] introuvable, ignore : {p}", file=sys.stderr)
                continue
            chemins.append(p)
        return chemins
    return sorted(
        p for p in list(args.racine.rglob("*.qgs")) + list(args.racine.rglob("*.qgz"))
        if BACKUP_DIRNAME not in p.parts
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("racine", type=Path, nargs="?", help="Dossier des projets a traiter")
    parser.add_argument("--liste", type=Path, help="Fichier texte listant les projets, un chemin par ligne")
    parser.add_argument("--from", dest="src", required=True, metavar="SERVICE", help="Service actuel")
    parser.add_argument("--to", dest="dst", required=True, metavar="SERVICE", help="Service cible")
    parser.add_argument("--apply", action="store_true", help="Ecrit reellement (defaut : simulation)")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=Path("journal_switch.csv"))
    args = parser.parse_args()

    if not args.racine and not args.liste:
        sys.exit("Indiquer soit un dossier racine, soit --liste.")
    if args.src == args.dst:
        sys.exit("--from et --to sont identiques : rien a faire.")
    if args.liste and args.apply and not args.backup_dir:
        sys.exit(
            "En mode --liste, indiquer explicitement --backup-dir : les projets "
            "peuvent venir de plusieurs dossiers, il n'y a pas d'emplacement de "
            "sauvegarde evident."
        )

    # Le sens de bascule figure dans le nom du dossier : c'est plus parlant a la
    # relecture qu'un horodatage seul, et deux executions rapprochees (aller
    # puis retour) ne peuvent pas ecraser mutuellement leurs sauvegardes.
    horodatage = f"{dt.datetime.now():%Y%m%d_%H%M%S}_{args.src}_vers_{args.dst}"
    # En simulation, --liste se passe de --backup-dir : rien n'est sauvegarde.
    base_sauvegarde = args.backup_dir or (args.racine / BACKUP_DIRNAME if args.racine else None)
    backup_dir = base_sauvegarde / horodatage if base_sauvegarde else None

    rows: list[dict] = []
    n_modifies = 0
    echecs: list[tuple[Path, str]] = []
    projets = collecter_projets(args)

    for path in projets:
        try:
            if path.suffix.lower() == ".qgz":
                new_bytes, nb = process_qgz(path, args.src, args.dst, rows)
            else:
                new_bytes, nb = process_xml(path.read_bytes(), args.src, args.dst, str(path), rows)
        except (etree.XMLSyntaxError, zipfile.BadZipFile, OSError) as exc:
            print(f"  [!] {path} : illisible ({exc})", file=sys.stderr)
            continue

        if nb == 0:
            continue
        n_modifies += 1
        if not args.apply:
            print(f"  [ ] {path} : {nb} couche(s) basculeraient")
            continue
        try:
            dest = backup_dir / nom_sauvegarde(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())
            write_atomic(path, new_bytes)
        except OSError as exc:
            print(f"  [!] {path} : ecriture impossible ({exc})", file=sys.stderr)
            echecs.append((path, str(exc)))
            n_modifies -= 1
            continue
        print(f"  [x] {path} : {nb} couche(s) basculee(s)")

    with args.log.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)

    par_statut: dict[str, int] = {}
    for r in rows:
        par_statut[r["statut"]] = par_statut.get(r["statut"], 0) + 1

    print(f"\n{len(projets)} projet(s) examines, {n_modifies} {'modifies' if args.apply else 'a modifier'}.")
    print(f"{args.src} -> {args.dst}")
    print(f"Journal : {args.log}\n")
    for statut, n in sorted(par_statut.items(), key=lambda kv: -kv[1]):
        print(f"  {statut} : {n}")

    if echecs:
        print(f"\n{len(echecs)} projet(s) non ecrits (verrouilles) :")
        for path, motif in echecs:
            print(f"  {path} : {motif}")

    if not args.apply:
        print("\nSimulation uniquement. Relancer avec --apply pour appliquer.")
    elif n_modifies:
        print(f"\nSauvegardes dans {backup_dir}")

    sys.exit(1 if echecs else 0)


if __name__ == "__main__":
    main()
