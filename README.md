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

You need Python 3.10 or newer. Check with `python3 --version` (macOS ships 3.9: install a newer one from
[python.org](https://www.python.org/downloads/) or with `brew install python`).

Install into a virtual environment, so it works with Homebrew and system Pythons too:

```bash
git clone https://github.com/techarkio/amazon-buybox-checker.git
cd amazon-buybox-checker
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install .
playwright install chromium
```

In a new terminal, run `source .venv/bin/activate` from the project folder again before using the commands below.

## Web page

```bash
buybox-checker-web
```

Your browser opens <http://localhost:8765>. Paste Amazon links or ASINs (or upload a .txt or .csv file), pick
the states, and press **Check buybox**. You can close the page while it runs and come back with its link.
Leave the terminal open: closing it stops the web page.

## Command line

```bash
buybox-checker B0BCMLGPYB https://www.amazon.com/dp/B09B8V1LZ3
buybox-checker --asin-file asins.csv --states core-growth
buybox-checker B0BCMLGPYB --states NY CA TX
```

Results are saved as a CSV in `~/.buybox-checker/results/`, with a summary per product in the terminal.

## States

Check all 50 states, a list of state codes, or a preset:

- `core`: CA, TX, FL, NY
- `core-growth`: the core states plus PA, IL, OH, GA, NC, MI, NJ, VA

## Troubleshooting

- **`error: externally-managed-environment`** when installing: you ran pip outside a virtual environment. Follow
  the Install steps above, with `python3 -m venv .venv` and `source .venv/bin/activate`.
- **`requires a different Python`** or **`command not found: buybox-checker-web`**: your Python is older than 3.10,
  or the virtual environment isn't active. Run `source .venv/bin/activate`, then `python --version`.
- **`address already in use`**: another program uses port 8765. Run `buybox-checker-web --port 8800`.
- **The browser doesn't open**: open <http://localhost:8765> yourself while `buybox-checker-web` is running.
- **`Executable doesn't exist`** when a check starts: run `playwright install chromium`.

## Contact

Need to check more products, more often, or from more places? Want this built into your own tools?
Contact TECHARK:

- Email: [hello@techark.io](mailto:hello@techark.io)
- Website: [www.techark.io](https://www.techark.io/#contact)

## License

MIT. Made by [TECHARK](https://www.techark.io).
