# RocketScrape

RocketScrape is a command line tool to process Discord message streams.

## Setup

To get started, make sure you have Python 3.11 or later installed. RocketScrape can be installed 
using setuptools by running:
```bash
pip install -e .
```

## Usage

The tool primarily expects a source (`--flag`) and analysis type (positional argument) to be specified. 
Below are examples of how to use RocketScrape:

```bash
rocketscrape -c CHANNEL1 [-c CHANNEL2, ...] ANALYSIS 
```
Replace `CHANNEL1`, `CHANNEL2`, etc., with either the Discord channel ID or one of the predefined named channels from 
the Channel enum in [rocketscrape.py](src/rocketscrape.py). Similarly, you can analyze an run it on an entire server by using:
```bash
rocketscrape --server SERVER ANALYSIS 
```
`SERVER` can either be a server ID or a name defined in the `Server` enum. There are optional `--start` and
`--end` arguments that accept an ISO format datetime string to restrict the date range. For instance,
to get the top contributors for the support channel over the last month, you can use:
```bash
rocketscrape -c support -s $(date -d "-1 month" +"%Y-%m-%d") contributors
```
For a complete list of global options and analysis types, run the help command:
```bash
rocketscrape -h
```
Additionally, you can access help commands for specific analysis types, such as:
```bash
rocketscrape contributor-history -h
```

## Adding a custom analysis class
Each analysis type in RocketScrape is implemented as a subclass of `MessageAnalysis`. These classes are required to 
implement `_prepare(self)`, `_on_message(self, message)`, `_finalize(self)`,
`display_result(self, result, client, max_results)` and `subcommand()`. Optionally, `custom_args(cls)` can be overridden
to specify analysis-specific command line arguments. Examples can be found in [analysis.py](src/analysis.py).