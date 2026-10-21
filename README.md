<p align="center">
  <a href="https://www.techark.io">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/techark-logo-dark.svg">
      <img src="assets/techark-logo.svg" alt="TECHARK" width="240">
    </picture>
  </a>
</p>

# Amazon Buybox Checker

See who wins the Amazon buybox for your products in each US state, from a web page or the command line.

The checker opens each product page as a shopper in every state you pick (one zipcode per state) and reads
the buybox: the seller under "Sold by" next to Add to Cart, the price, shipping and delivery. You get a map,
a list of the products that need attention, and a CSV.

> Checks run from your own computer and IP address. Keep volumes low and respect Amazon's Conditions of Use.
> Not affiliated with Amazon.

## Install

Runs on macOS, Windows and Linux, with Python 3.10 or newer. The steps install it into a virtual environment
(`.venv`), so it doesn't mix with other Python setups such as Homebrew or conda.

### macOS and Linux

```bash
git clone https://github.com/techarkio/amazon-buybox-checker.git
cd amazon-buybox-checker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m playwright install chromium
```

- **macOS**: check `python3 --version`. The Python that ships with macOS is 3.9, which is too old: install a newer
  one from [python.org](https://www.python.org/downloads/) or with `brew install python`.
- **Linux** (Ubuntu, Debian): install venv support first with `sudo apt install python3-venv`, and install the
  browser with its system libraries: `python -m playwright install --with-deps chromium` (it asks for your password).

### Windows

In PowerShell, with Python from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH")
and [Git](https://git-scm.com/download/win) installed:

```powershell
git clone https://github.com/techarkio/amazon-buybox-checker.git
cd amazon-buybox-checker
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install .
python -m playwright install chromium
```

If PowerShell says running scripts is disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once,
or use Command Prompt and `.venv\Scripts\activate.bat` instead.

### Every new terminal

Activate the virtual environment from the project folder before using the commands below:
`source .venv/bin/activate` on macOS and Linux, `.venv\Scripts\Activate.ps1` on Windows.

## Web page

```bash
buybox-checker-web
```

Your browser opens <http://localhost:8765>. Paste Amazon links or ASINs (or upload a .txt or .csv file), pick
the states, and press **Check buybox**. You can close the page while it runs: it opens again on the running check.
Finished checks are listed under **Past checks**, with their CSV. Leave the terminal open: closing it stops the
web page.

## Command line

```bash
buybox-checker B0BCMLGPYB https://www.amazon.com/dp/B09B8V1LZ3
buybox-checker --asin-file asins.csv --states core-growth
buybox-checker B0BCMLGPYB --states NY CA TX
```

Results are saved as a CSV in `~/.buybox-checker/results/` (on Windows, `%USERPROFILE%\.buybox-checker\results\`),
with a summary per product in the terminal. Set `BUYBOX_CHECKER_HOME` to keep them somewhere else.

## States

Check all 50 states, a list of state codes, or a preset:

- `core`: CA, TX, FL, NY
- `core-growth`: the core states plus PA, IL, OH, GA, NC, MI, NJ, VA

## Troubleshooting

- **`error: externally-managed-environment`** when installing: you ran pip outside a virtual environment. Follow
  the Install steps above, with `python3 -m venv .venv` and `source .venv/bin/activate`.
- **`requires a different Python`**, **`command not found: buybox-checker-web`** or **`is not recognized`**
  (Windows): your Python is older than 3.10, or the virtual environment isn't active. Activate it (see
  [Every new terminal](#every-new-terminal)), then check `python --version`.
- **`address already in use`**: another program uses port 8765. Run `buybox-checker-web --port 8800`.
- **The browser doesn't open**: open <http://localhost:8765> yourself while `buybox-checker-web` is running.
- **`Executable doesn't exist`** when a check starts: the browser for this Playwright version isn't installed.
  With the virtual environment active, run `python -m playwright install chromium`. A bare `playwright install`
  can run another environment's Playwright (conda, for example) and download a browser this one can't use.
- **`error while loading shared libraries`** or **`Host system is missing dependencies`** (Linux): run
  `python -m playwright install --with-deps chromium`.

## Contact

Need to check more products, more often, or from more places? Want this built into your own tools?
Contact TECHARK:

- Email: [hello@techark.io](mailto:hello@techark.io)
- Website: [www.techark.io](https://www.techark.io/#contact)

## License

MIT. Made by [TECHARK](https://www.techark.io).
