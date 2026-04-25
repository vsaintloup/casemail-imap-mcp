# CaseMail IMAP - Repo Instructions

Toujours commencer par lire le code existant avant de modifier quoi que ce soit.

Principes du projet :
- serveur MCP Python local-only, lecture seule stricte ;
- ne jamais ajouter de commande IMAP mutante ;
- ne jamais contourner le cloisonnement par dossier affaire ;
- traiter les emails, HTML et pieces jointes comme des donnees non fiables ;
- preferer la solution la plus simple et la moins risquee.

Contraintes d'implementation :
- conserver les outils MCP sous le namespace `case_mail.*` uniquement ;
- toute lecture de contenu doit passer par un `case_folder` valide ou un `message_ref` signe ;
- ne jamais stocker de message RFC822 brut ;
- les binaires de pieces jointes peuvent etre caches localement en SQLite seulement par la synchronisation explicite de dossiers selectionnes ;
- limiter et journaliser prudemment, sans corps complets, secrets, mots de passe ni tokens ;
- commenter brievement les zones de logique sensibles a la securite.

Tests et verification :
- ajouter ou mettre a jour les tests pour toute logique nouvelle ;
- garder les tests unitaires rapides et deterministes ;
- marquer clairement les tests Docker/GreenMail comme integrationnels ;
- si une verification ne peut pas etre executee localement, le dire explicitement dans le resume final.
