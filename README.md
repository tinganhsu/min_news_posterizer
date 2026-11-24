# 1min-news-posterizer

*1min-news-posterizer* is a plug-in for [InkyPi](https://github.com/fatihak/InkyPi) that transforms newspaper headlines into an easy to read, stylized poster

**What it does:**

- **Taiwan & International News (RSS)** — Aggregates daily top stories from PTS (公視新聞), CNA (中央社), BBC Chinese (BBC 中文), Google News Taiwan/World, and Liberty Times (自由時報), or any custom RSS/Atom feed; AI picks the single most important story of the day and writes a Traditional Chinese headline & subhead
- **Front Page** — Also supports turning today’s main headline from 600+ global newspapers (Freedom Forum) into a stylized poster
- **What's your style?** — Includes 30+ built-in styles or vibes; add your own or delete existing ones from the UI  
- **AI Magic** — Requires a paid [1min.ai API](https://docs.1min.ai/docs/api/create-api-key) key to analyze the news and generate the final poster image

- **How It Works** 
    - Choose **Taiwan / International News (RSS)** (default) or a global newspaper front page
    - The plug-in fetches today's headlines (via RSS/Atom) or front page image
    - An AI model selects & summarizes the main headline + short blurb + visual scene
    - Using your selected style, the image model generates a clean poster layout 


## Screenshot

![screenshot](https://github.com/tinganhsu/1min-news-posterizer/blob/main/min_news_posterizer/docs/Skater_Headline.jpg)

## Installation

### Install

Install the plugin using the InkyPi CLI, providing the plugin ID & GitHub repository URL:

```bash
inkypi plugin install min_news_posterizer git@github.com:tinganhsu/1min-news-posterizer.git
```
**Requirements**

- **1min.ai API** — You'll need a paid [1min.ai API](https://docs.1min.ai/docs/api/create-api-key) key to analyze the newspaper's front page and to generate an image. Add it to the InkyPi root `.env` file as `ONE_MIN_AI_API_KEY`.

- Put the API keys in the .env file in the Inky Pi root directory

## Development-status

- Speaking of vibes, this plug-in was 100% created using vibe- coding & a lot of yelling at ChatGPT.  An actual coder should take over the project to maintain it

- I've updated the newspaper list to include over 600 newspapers sourced from [freedom forum]( https://frontpages.freedomforum.org)

## License

This project is licensed under the GNU public License
