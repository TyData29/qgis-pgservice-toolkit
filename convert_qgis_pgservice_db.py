#!/usr/bin/env python3
"""Conversion vers `pg_service.conf` des projets QGIS stockes en base PostgreSQL.

Pendant de convert_qgis_pgservice.py pour les projets enregistres dans une
table qgis_projects (stockage PostgreSQL de QGIS). Meme regle de conversion,
memes statuts de couche, meme journal CSV : voir ce script pour le detail.

La colonne content contient le projet au format .qgz (archive zip). Le contenu
de chaque projet est controle a chaque lancement : s'il ne commence pas par la
signature zip (PK), le projet est laisse intact et journalise avec le statut
"contenu_non_qgz". Seule la colonne content est modifiee.

Schemas : par defaut, tous les schemas visibles qui contiennent une table
qgis_projects. --schema (repetable) restreint le parcours, --projet
(repetable) restreint aux projets de ce nom, pour un panel de test.

Sauvegarde : avant toute modification, la ligne d'origine est copiee dans
<schema>.qgis_projects_bak, creee au besoin avec les colonnes de qgis_projects
plus sauvegarde_le et lot. Le lot identifie l'execution. QGIS ne liste pas
cette table, les sauvegardes n'apparaissent donc pas dans le navigateur.
Retour arriere d'une execution :
    update <schema>.qgis_projects p
    set content = b.content, metadata = b.metadata
    from <schema>.qgis_projects_bak b
    where b.name = p.name and b.lot = '<lot>';

Connexion : --conn recoit une chaine de connexion libpq, par exemple
"host=10.0.0.5 port=5432 dbname=sig user=admin_sig" ou "service=nom". Le mot
de passe est demande a l'execution si le serveur en exige un et qu'il n'est
fourni ni par la chaine, ni par pgpass.conf ; -W le demande d'emblee. Le role
doit pouvoir lire et mettre a jour les tables qgis_projects visees, et creer
la table de sauvegarde dans leur schema. A ne pas confondre avec --dbname, qui
filtre les couches des projets d'apres leur propre dbname=.

Chaque projet est traite dans sa propre transaction, ligne verrouillee. Un
projet ouvert par un agent pendant la conversion sera ecrase par sa version
non convertie s'il l'enregistre ensuite : lancer la conversion projets fermes.

Usage :
    python convert_qgis_pgservice_db.py --conn "host=... dbname=... user=..." [-W]
        [--schema qgis] [--projet NOM] [--service prod]
        [--exclude-host IP] [--dbname NOM] [--apply] [--log FICHIER]

Le mode par defaut est une simulation : rien n'est ecrit en base.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import getpass
import sys
import zipfile
from pathlib import Path

try:
    import psycopg
    from psycopg import sql
except ImportError:
    try:
        import psycopg2 as psycopg
        from psycopg2 import sql
    except ImportError:
        sys.exit('Ce script necessite psycopg (pip install "psycopg[binary]") ou psycopg2.')

from convert_qgis_pgservice import LOG_FIELDS, etree, process_qgz_bytes

ZIP_SIGNATURE = b"PK\x03\x04"
TABLE_PROJETS = "qgis_projects"
TABLE_SAUVEGARDE = "qgis_projects_bak"


def connecter(conninfo: str, demander: bool):
    """Ouvre la connexion. Le mot de passe est demande si -W est passe, ou si
    le serveur le refuse ou l'exige alors qu'aucun n'a ete fourni. Le test
    porte sur le texte du message, en anglais ou en francais selon la langue
    de libpq et du serveur."""
    if demander:
        return psycopg.connect(conninfo, password=getpass.getpass("Mot de passe : "))
    try:
        return psycopg.connect(conninfo)
    except psycopg.OperationalError as exc:
        message = str(exc).lower()
        if not sys.stdin.isatty() or ("password" not in message and "mot de passe" not in message):
            raise
    return psycopg.connect(conninfo, password=getpass.getpass("Mot de passe : "))


def ligne_projet(etiquette: str, statut: str) -> dict[str, str]:
    """Ligne de journal pour un statut qui concerne le projet entier."""
    row = dict.fromkeys(LOG_FIELDS, "")
    row["chemin_projet"] = etiquette
    row["statut"] = statut
    return row


def schemas_avec_projets(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select table_schema
            from information_schema.columns
            where table_name = %s
              and column_name in ('name', 'content')
            group by table_schema
            having count(*) = 2
            order by table_schema
            """,
            (TABLE_PROJETS,),
        )
        schemas = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return schemas


def creer_table_sauvegarde(conn, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "create table if not exists {}.{} ("
                "like {}.{}, "
                "sauvegarde_le timestamptz not null default now(), "
                "lot text not null)"
            ).format(
                sql.Identifier(schema),
                sql.Identifier(TABLE_SAUVEGARDE),
                sql.Identifier(schema),
                sql.Identifier(TABLE_PROJETS),
            )
        )
    conn.commit()


def lister_projets(conn, schema: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("select name from {}.{} order by name").format(
                sql.Identifier(schema), sql.Identifier(TABLE_PROJETS)
            )
        )
        noms = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return noms


def traiter_projet(conn, schema: str, nom: str, args, lot: str, rows: list[dict]) -> str:
    """Retourne l'issue du traitement : rien, simulation, converti, non_qgz,
    illisible. Les erreurs de base et d'ecriture remontent en exception, la
    transaction etant annulee par l'appelant."""
    etiquette = f"{schema}.{TABLE_PROJETS}/{nom}"
    projets = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(TABLE_PROJETS))
    sauvegarde = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(TABLE_SAUVEGARDE))

    with conn.cursor() as cur:
        # En --apply, la ligne reste verrouillee jusqu'au commit : un
        # enregistrement concurrent depuis QGIS attend, ou le script echoue
        # apres lock_timeout.
        lecture = "select content from {} where name = %s"
        if args.apply:
            lecture += " for update"
        cur.execute(sql.SQL(lecture).format(projets), (nom,))
        ligne = cur.fetchone()
        if ligne is None or ligne[0] is None:
            # Projet supprime entre le listage et la lecture, ou contenu vide.
            conn.rollback()
            return "rien"
        contenu = bytes(ligne[0])

        if not contenu.startswith(ZIP_SIGNATURE):
            rows.append(ligne_projet(etiquette, "contenu_non_qgz"))
            conn.rollback()
            return "non_qgz"

        try:
            new_bytes, nb = process_qgz_bytes(
                contenu, etiquette, args.service, set(args.exclude_host), rows, set(args.dbnames) or None
            )
        except (etree.XMLSyntaxError, zipfile.BadZipFile) as exc:
            print(f"  [!] {etiquette} : illisible ({exc.__class__.__name__}: {exc})", file=sys.stderr)
            rows.append(ligne_projet(etiquette, "contenu_illisible"))
            conn.rollback()
            return "illisible"

        if nb == 0:
            conn.rollback()
            return "rien"
        if not args.apply:
            print(f"  [ ] {etiquette} : {nb} couche(s) seraient converties (simulation)")
            conn.rollback()
            return "simulation"

        # Copie cote serveur de la version verrouillee, dans la meme
        # transaction que la mise a jour : pas de modification sans sauvegarde.
        cur.execute(
            sql.SQL(
                "insert into {} (name, metadata, content, lot) "
                "select name, metadata, content, %s from {} where name = %s"
            ).format(sauvegarde, projets),
            (lot, nom),
        )
        cur.execute(sql.SQL("update {} set content = %s where name = %s").format(projets), (new_bytes, nom))
    conn.commit()
    print(f"  [x] {etiquette} : {nb} couche(s) convertie(s)")
    return "converti"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--conn",
        required=True,
        help='Chaine de connexion libpq (ex: "host=10.0.0.5 port=5432 dbname=sig user=admin_sig")',
    )
    parser.add_argument(
        "-W",
        "--password",
        dest="demander_mdp",
        action="store_true",
        help="Demande le mot de passe avant de se connecter",
    )
    parser.add_argument(
        "--schema",
        dest="schemas",
        action="append",
        default=[],
        metavar="NOM",
        help="Schema contenant une table qgis_projects, repetable (defaut : tous)",
    )
    parser.add_argument(
        "--projet",
        dest="projets",
        action="append",
        default=[],
        metavar="NOM",
        help="Nom de projet a traiter, repetable (defaut : tous)",
    )
    parser.add_argument("--service", default="prod", help="Nom du service pg_service.conf a poser (defaut : prod)")
    parser.add_argument(
        "--exclude-host",
        action="append",
        default=[],
        metavar="IP",
        help="Host a ne pas convertir, repetable (ex: --exclude-host 203.0.113.10)",
    )
    parser.add_argument(
        "--dbname",
        dest="dbnames",
        action="append",
        default=[],
        metavar="NOM",
        help="Restreint la conversion aux couches dont le dbname= figure dans cette liste, repetable",
    )
    parser.add_argument("--apply", action="store_true", help="Ecrit reellement en base (defaut : simulation)")
    parser.add_argument("--dry-run", action="store_true", help="Simulation explicite (comportement par defaut)")
    parser.add_argument("--log", type=Path, default=Path("journal_conversion_db.csv"))
    args = parser.parse_args()

    if args.apply and args.dry_run:
        sys.exit("--apply et --dry-run sont contradictoires : choisir l'un ou l'autre.")

    lot = f"{dt.datetime.now():%Y%m%d_%H%M%S}_{args.service}"

    try:
        conn = connecter(args.conn, args.demander_mdp)
    except psycopg.Error as exc:
        sys.exit(f"Connexion impossible : {exc}")

    rows: list[dict[str, str]] = []
    issues: dict[str, int] = {}
    echecs: list[tuple[str, str]] = []
    projets_trouves: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute("set lock_timeout = '10s'")
        conn.commit()

        disponibles = schemas_avec_projets(conn)
        if args.schemas:
            absents = [s for s in args.schemas if s not in disponibles]
            if absents:
                sys.exit(f"Pas de table {TABLE_PROJETS} lisible dans : {', '.join(absents)}")
            schemas = args.schemas
        else:
            schemas = disponibles
        if not schemas:
            sys.exit(f"Aucune table {TABLE_PROJETS} lisible avec cette connexion.")

        for schema in schemas:
            print(f"Schema {schema}")
            noms = lister_projets(conn, schema)
            if args.projets:
                noms = [n for n in noms if n in args.projets]
            if not noms:
                continue
            if args.apply:
                creer_table_sauvegarde(conn, schema)
            for nom in noms:
                projets_trouves.add(nom)
                try:
                    issue = traiter_projet(conn, schema, nom, args, lot, rows)
                except psycopg.Error as exc:
                    conn.rollback()
                    motif = f"{exc.__class__.__name__}: {str(exc).strip()}"
                    print(f"  [!] {schema}.{TABLE_PROJETS}/{nom} : ecriture impossible ({motif})", file=sys.stderr)
                    echecs.append((f"{schema}.{TABLE_PROJETS}/{nom}", motif))
                    continue
                issues[issue] = issues.get(issue, 0) + 1
    finally:
        conn.close()

    with args.log.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    par_statut: dict[str, int] = {}
    for row in rows:
        par_statut[row["statut"]] = par_statut.get(row["statut"], 0) + 1

    n_modifies = issues.get("converti", 0) + issues.get("simulation", 0)
    print(f"\n{sum(issues.values()) + len(echecs)} projet(s) parcourus dans {len(schemas)} schema(s).")
    print(f"{n_modifies} projet(s) {'convertis' if args.apply else 'a convertir'}.")
    print(f"Service pose : {args.service}")
    if args.exclude_host:
        print(f"Hosts exclus : {', '.join(sorted(set(args.exclude_host)))}")
    if args.dbnames:
        print(f"Bases visees : {', '.join(sorted(set(args.dbnames)))}")
    print(f"Journal detaille : {args.log}\n")
    print("Repartition par statut :")
    for statut, n in sorted(par_statut.items(), key=lambda kv: -kv[1]):
        print(f"  {statut} : {n}")

    manquants = sorted(set(args.projets) - projets_trouves)
    if manquants:
        print(f"\nProjet(s) demandes introuvables : {', '.join(manquants)}")

    if issues.get("non_qgz") or issues.get("illisible"):
        print(
            f"\n{issues.get('non_qgz', 0) + issues.get('illisible', 0)} projet(s) au contenu non reconnu, "
            "laisses intacts (statuts contenu_non_qgz et contenu_illisible dans le journal)."
        )

    if echecs:
        print(f"\n{len(echecs)} projet(s) non ecrits :")
        for etiquette, motif in echecs:
            print(f"  {etiquette} : {motif}")
        print("Relancer le script sur ces projets une fois qu'ils sont fermes.")

    a_examiner = sum(par_statut.get(s, 0) for s in ("sans_host", "erreur_transformation"))
    if a_examiner:
        print(f"\n{a_examiner} couche(s) a examiner manuellement (voir statuts dans le journal).")

    if not args.apply:
        print("\nSimulation uniquement, rien n'a ete ecrit. Relancer avec --apply pour convertir reellement.")
    elif issues.get("converti"):
        print(f"\nOriginaux sauvegardes dans <schema>.{TABLE_SAUVEGARDE}, lot {lot}")
    else:
        print("\nAucun projet a convertir : rien n'a ete modifie ni sauvegarde.")

    sys.exit(1 if echecs else 0)


if __name__ == "__main__":
    main()
