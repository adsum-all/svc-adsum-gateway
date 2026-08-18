-- Le registre de la passerelle : ce qui est parti, par où, et avec quel résultat.
--
-- Trois tables, et aucune ne porte d'adresse en clair. Le registre est consulté par
-- des exploitants qui n'ont aucune raison de lire l'adresse d'un administrateur
-- client, et une base aspirée ailleurs ne doit pas devenir un fichier d'adresses.
-- Le condensé répond à la seule question utile : avons-nous écrit à celle-ci.

-- Ces tables se posent dans le schéma courant, jamais dans un schéma nommé ici.
--
-- Le nom appartient au script d'application, `deployment/database/creer_schema_passerelle.py`,
-- qui crée le schéma puis pose le chemin de recherche avant d'appliquer ce fichier.
-- Coder « passerelle » en dur ici empêcherait d'appliquer la migration dans un
-- schéma de test ou dans un second environnement, et le jour où quelqu'un le
-- contourne, il le contourne mal.
--
-- Ce qui compte, et que le script garantit : ces tables ne doivent jamais atterrir
-- dans « public ». L'hébergement y expose une API automatique accessible à un rôle
-- anonyme, et le registre des envois de l'éditeur y deviendrait lisible sans
-- authentification.

CREATE TABLE envoi (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- La clé portée par l'appelant. Unique : c'est elle qui empêche qu'une mise en
    -- demeure parte deux fois quand un délai dépassé fait réessayer.
    cle_idempotence   text NOT NULL UNIQUE,
    canal             text NOT NULL,
    -- SHA-256 de l'adresse normalisée. Jamais l'adresse.
    adresse_empreinte text NOT NULL,
    categorie         text NOT NULL DEFAULT 'transactionnel',
    -- L'organisation concernée, vide pour un message qui ne concerne personne en
    -- particulier. Sert à répondre à « qu'avons-nous envoyé à ce client ».
    organisation      text NOT NULL DEFAULT '',
    statut            text NOT NULL DEFAULT 'en_attente',
    fournisseur       text,
    -- L'identifiant rendu par le fournisseur. Seul moyen de rapprocher un accusé
    -- reçu plus tard de l'envoi qui l'a provoqué.
    reference         text,
    tentatives        smallint NOT NULL DEFAULT 0,
    envoye_le         timestamptz,
    motif             text,
    cree_le           timestamptz NOT NULL DEFAULT now(),
    maj_le            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT envoi_statut_ok CHECK (statut IN (
        'en_attente', 'accepte', 'remis', 'refuse', 'bloque')),
    CONSTRAINT envoi_canal_ok CHECK (canal IN (
        'courriel', 'telegram', 'sms', 'poussee')),
    -- Un envoi qui a abouti porte sa date. Les trois états où rien n'est parti en
    -- sont dispensés, « bloque » compris : un destinataire désabonné est constaté
    -- avant l'envoi, ou refusé par le fournisseur sans que rien ne parte. L'oubli
    -- de « bloque » ici faisait échouer l'écriture du blocage, donc laissait passer
    -- l'envoi suivant vers la même adresse morte.
    CONSTRAINT envoi_date_si_envoye CHECK (
        statut IN ('en_attente', 'refuse', 'bloque') OR envoye_le IS NOT NULL)
);

-- Le rapprochement d'un accusé se fait sur la référence du fournisseur. Sans cet
-- index, chaque accusé balaie la table entière, et ils arrivent par milliers.
CREATE INDEX envoi_par_reference ON envoi (fournisseur, reference)
    WHERE reference IS NOT NULL;

-- Les deux lectures d'exploitation : le fil récent, et le fil d'un client.
CREATE INDEX envoi_par_date ON envoi (cree_le DESC);
CREATE INDEX envoi_par_organisation ON envoi (organisation, cree_le DESC)
    WHERE organisation <> '';


CREATE TABLE adresse_bloquee (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canal             text NOT NULL,
    adresse_empreinte text NOT NULL,
    motif             text NOT NULL,
    bloquee_le        timestamptz NOT NULL DEFAULT now(),
    -- Par canal : une adresse morte en courriel n'a rien à voir avec une
    -- conversation Telegram fermée, et bloquer l'une ne doit pas fermer l'autre.
    CONSTRAINT adresse_bloquee_unique UNIQUE (canal, adresse_empreinte),
    CONSTRAINT adresse_bloquee_canal_ok CHECK (canal IN (
        'courriel', 'telegram', 'sms', 'poussee'))
);


-- Les accusés déjà appliqués. La déduplication est portée par cette contrainte et
-- non par une lecture préalable : entre un SELECT et un INSERT, une seconde
-- livraison du même accusé passe.
CREATE TABLE accuse (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    fournisseur            text NOT NULL,
    identifiant_evenement  text NOT NULL,
    statut                 text NOT NULL,
    recu_le                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT accuse_unique UNIQUE (fournisseur, identifiant_evenement)
);


-- Une ligne par tentative, y compris les échouées. C'est ce qui permet de dire
-- « le premier fournisseur a refusé, le second a pris » plutôt que de ne constater
-- que le résultat final, et donc de voir un canal se dégrader avant la réclamation.
CREATE TABLE tentative (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cle_idempotence text NOT NULL,
    fournisseur     text NOT NULL,
    resultat        text NOT NULL,
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    faite_le        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tentative_par_cle ON tentative (cle_idempotence, faite_le);


-- Fermeture des accès par défaut.
--
-- Défense en profondeur derrière le cloisonnement par schéma : même si quelqu'un
-- ajoute un jour « passerelle » aux schémas exposés, les rôles anonymes n'y ont
-- aucun droit. Un REVOKE coûte une ligne et se lit ; un oubli se paie en fuite.
DO $$
DECLARE courant text := current_schema();
BEGIN
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', courant);
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', courant);
END $$;

-- La sécurité au niveau des lignes, forcée.
--
-- « FORCE » et non seulement « ENABLE » : la connexion applicative est propriétaire
-- de ce schéma, et un propriétaire contourne la sécurité au niveau des lignes tant
-- qu'elle n'est pas forcée. Sans le mot, la protection existe sur le papier et ne
-- s'applique à personne.
ALTER TABLE envoi            ENABLE ROW LEVEL SECURITY;
ALTER TABLE envoi            FORCE  ROW LEVEL SECURITY;
ALTER TABLE adresse_bloquee  ENABLE ROW LEVEL SECURITY;
ALTER TABLE adresse_bloquee  FORCE  ROW LEVEL SECURITY;
ALTER TABLE accuse           ENABLE ROW LEVEL SECURITY;
ALTER TABLE accuse           FORCE  ROW LEVEL SECURITY;
ALTER TABLE tentative        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tentative        FORCE  ROW LEVEL SECURITY;

-- Le service lit et écrit tout : il est la seule application de ce schéma, et son
-- cloisonnement se fait par son secret d'appel, pas par la base. La politique est
-- nommée et permissive à dessein plutôt qu'absente : une table en RLS sans aucune
-- politique refuse tout, y compris au service, et la panne se manifeste par un
-- registre qui paraît vide.
CREATE POLICY service_passerelle ON envoi           USING (true) WITH CHECK (true);
CREATE POLICY service_passerelle ON adresse_bloquee USING (true) WITH CHECK (true);
CREATE POLICY service_passerelle ON accuse          USING (true) WITH CHECK (true);
CREATE POLICY service_passerelle ON tentative       USING (true) WITH CHECK (true);
