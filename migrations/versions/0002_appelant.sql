-- Qui a demandé cet envoi.
--
-- Le registre disait ce qui était parti, jamais qui l'avait demandé. Avec un secret
-- unique partagé par tous les services appelants, cette question n'avait pas de
-- réponse : un service compromis pouvait écrire à n'importe quelle adresse sous
-- l'identité de l'éditeur, et rien dans le registre n'aurait permis de dire lequel.
--
-- La colonne est renseignée depuis le secret présenté, jamais depuis le corps de la
-- requête : une valeur que l'appelant choisit lui-même ne prouve rien.

ALTER TABLE envoi ADD COLUMN appelant text NOT NULL DEFAULT 'inconnu';

COMMENT ON COLUMN envoi.appelant IS
    'Le service qui a demandé l''envoi, déduit du secret présenté. « inconnu » pour
     les appels faits avec le secret partagé historique.';

-- La question d'exploitation qui suit un incident : qu'a demandé ce service, et
-- quand. Sans index, elle balaie tout le registre.
CREATE INDEX envoi_par_appelant ON envoi (appelant, cree_le DESC);
