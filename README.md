# park - Personal Action Runner Kontainers  

Do you know what I find annoying? If you're going to self host your GitHub actions runners for all your repos,
there is no standard way to automatically manage one runner for all your repos.
`park` is a scheduled task that automatically keeps track of your active repos and spins up instances for active repos
and stops instances for inactive repos. Maybe Microsoft will add self-hosted runners connected to accounts,
but until then, this is the best option for personal runners.

Requirements:  

`GitHub CLI`, `uv`, `docker`, `cron`  

## Setup  

Make sure your user is added to the docker group before starting.  
This can often be done like
```shell
sudo usermod -aG docker "$USER"
```

Log in and clone repository:
```shell
gh auth login
gh repo clone marksverdhei/park
cd park
```

Then install modules with uv
```shell
uv sync && source .venv/bin/activate
```
You can testrun the task first:
```shell
python main.py
```

Finally, add the task to your crontab.  
`crontab -e`

If you have cloned the repo in `/home/me/park`, you can 
`0 * * * * /home/me/park/.venv/bin/python /home/me/park/main.py`
