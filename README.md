# Amazon Buybox Checker

See who wins the Amazon buybox for your products in each US state, from a web page or the command line.

The checker opens each product page as a shopper in every state you pick (one zipcode per state) and reads
the buybox: the seller under "Sold by" next to Add to Cart, the price, shipping and delivery. You get a map,
a list of the products that need attention, and a CSV.

> Checks run from your own computer and IP address. Keep volumes low and respect Amazon's Conditions of Use.
> Not affiliated with Amazon.

## Install

Python 3.10 or newer:

```bash
git clone https://github.com/techarkio/amazon-buybox-checker.git
cd amazon-buybox-checker
pip install .
playwright install chromium
```

## Web page

```bash
buybox-checker-web
```

Your browser opens <http://localhost:8765>. Paste Amazon links or ASINs (or upload a .txt or .csv file), pick
the states, and press **Check buybox**. You can close the page while it runs and come back with its link.

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

## License

MIT. Made by [TECHARK](https://www.techark.io).
