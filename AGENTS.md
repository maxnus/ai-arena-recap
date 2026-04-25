The goal is to make a website which shows matches and statistics that are played on aiarena.net.

- aiarena.net offers an API to query information

- The API token is in .env

- the website should at least have the following pages:

	- one overview page for the current "melee ladder" competition (currently number 36).
	  This page should should the current ranking, ELO, ELO changes, etc for each bot
	- one page for each bot that competes in the competition. This should have a match history as well
	  as the description of the bot and interesting statistics
	- one page for each match in the competition. This page should show the bots, match duration, and have a download button for the replay.
	  Later we will want to add the functionality to read replays and analyse them, so we willw ant to display statistics and graphs on this page later on
	  
- the website should work if aiarena.net is down (except download links); this probably means we have to store data in our own database?
