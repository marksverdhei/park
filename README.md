# park - Personal Action Runner Kontainers  

Do you know what I find annoying? If you're going to self host your GitHub actions runners for all your repos,
there is no standard way to automatically manage one runner for all your repos.
`park` is a scheduled task that automatically keeps track of your active repos and spins up instances for active repos
and stops instances for inactive repos.

Requirements:  

`GitHub CLI`  
`uv`
`docker`
`cron`

Clone this repo,  
`uv sync`  
`gh auth login`  

`crontab -e`

If you have cloned the repo in `/home/me/park`, you can 
`0 * * * * /home/me/park/.venv/bin/python /home/me/park/main.py`
