import calendar
import json
import os
import time
import sys
import textwrap
import datetime
from email.utils import formatdate, parsedate
from urllib.parse import urlparse

import appdirs
import click
import github3
import pipdate
import requests
from urllib3.util.retry import Retry
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import BaseHeuristic
from cachecontrol import CacheControl, CacheControlAdapter
from halo import Halo
from selectolax.parser import HTMLParser
from tabulate import tabulate

from ghtopdep import __version__

PACKAGE_NAME = "ghtopdep"
CACHE_DIR = appdirs.user_cache_dir(PACKAGE_NAME)
NEXT_BUTTON_SELECTOR = "#dependents > div.paginate-container > div > a"
ITEM_SELECTOR = "#dependents > div.Box > div.flex-items-center"
REPO_SELECTOR = "span > a.text-bold"
STARS_SELECTOR = "div > span:nth-child(1)"
GITHUB_URL = "https://github.com"

if pipdate.needs_checking(PACKAGE_NAME):
    msg = pipdate.check(PACKAGE_NAME, __version__.__version__)
    click.echo(msg)


class OneDayHeuristic(BaseHeuristic):
    cacheable_by_default_statuses = {
        200, 203, 204, 206, 300, 301, 404, 405, 410, 414, 501
    }

    def update_headers(self, response):
        if response.status not in self.cacheable_by_default_statuses:
            return {}

        date = parsedate(response.headers["date"])
        expires = datetime.datetime(*date[:6]) + datetime.timedelta(days=1)
        return {"expires": formatdate(calendar.timegm(expires.timetuple())), "cache-control": "public"}

    def warning(self, response):
        msg = "Automatically cached! Response is Stale."
        return "110 - {0}".format(msg)


def already_added(repo_url, repos):
    for repo in repos:
        if repo['url'] == repo_url:
            return True


def sort_repos(repos, rows):
    sorted_repos = sorted(repos, key=lambda i: i["stars"], reverse=True)
    return sorted_repos[:rows]


def humanize(num):
    if num < 1_000:
        return num
    elif num < 10_000:
        return "{}K".format(round(num / 100) / 10)
    elif num < 1_000_000:
        return "{}K".format(round(num / 1_000))
    else:
        return num


def readable_stars(repos):
    for repo in repos:
        repo["stars"] = humanize(repo["stars"])
    return repos


def show_result(repos, total_repos_count, more_than_zero_count, destinations, number_of_files_processed):
    print("boom")
    with open(f'output/output-{number_of_files_processed}.json', 'w') as outfile:
        json.dump(repos, outfile)

def get_page_url(sess, url, destination):
    page_url = "{0}/network/dependents?dependent_type={1}".format(url, destination.upper())
    main_response = sess.get(page_url)
    parsed_node = HTMLParser(main_response.text)
    link = parsed_node.css('.select-menu-item')
    if link:
        packages = []
        for i in link:
            repo_url = "https://github.com/{}".format(i.attributes['href'])
            res = requests.get(repo_url)
            parsed_item = HTMLParser(res.text)
            package_id = urlparse(i.attributes["href"]).query.split("=")[1]
            selector = '.table-list-filters a:first-child'
            count = parsed_item.css(selector)[0].text().split()[0].replace(",", "")
            packages.append({"count": int(count), "package_id": package_id})
        sorted_packages = sorted(packages, key=lambda k: k['count'], reverse=True)
        most_popular_package_id = sorted_packages[0].get("package_id")
        page_url = "{0}/network/dependents?dependent_type={1}&package_id={2}".format(url, destination.upper(),
                                                                                     most_popular_package_id)
    return page_url


@click.command()
@click.argument("url")
@click.option("--repositories/--packages", default=True, help="Sort repositories or packages (default repositories)")
@click.option("--rows", default=10, help="Number of showing repositories (default=10)")
@click.option("--minstar", default=5, help="Minimum number of stars (default=5)")
@click.option("--search", help="search code at dependents (repositories/packages)")
@click.option("--token", envvar="GHTOPDEP_TOKEN")

def cli(url, repositories, search, rows, minstar, token):
    MODE = os.environ.get("GHTOPDEP_ENV")
    REPOS_PER_FILE_SIZE_LIMIT = 500

    if (search) and token:
        gh = github3.login(token=token)
        CacheControl(gh.session,
                     cache=FileCache(CACHE_DIR),
                     heuristic=OneDayHeuristic())
    elif (search) and not token:
        click.echo("Please provide token")
        sys.exit()

    destination = "repository"
    destinations = "repositories"
    if not repositories:
        destination = "package"
        destinations = "packages"

    repos = []
    more_than_zero_count = 0
    total_repos_count = 0
    # spinner = Halo(text="Fetching information about {0}".format(destinations), spinner="dots")
    # spinner.start()




    sess = requests.session()
    retries = Retry(
        total=15,
        backoff_factor=15,
        status_forcelist=[429])
    adapter = CacheControlAdapter(max_retries=retries,
                                  cache=FileCache(CACHE_DIR),
                                  heuristic=OneDayHeuristic())
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    page_url = get_page_url(sess, url, destination)

    found_repos = 0
    number_of_files_processed = 0

    while True:
        time.sleep(1)
        response = sess.get(page_url)

        print(page_url)

        parsed_node = HTMLParser(response.text)
        dependents = parsed_node.css(ITEM_SELECTOR)
        total_repos_count += len(dependents)
        for dep in dependents:
            repo_stars_list = dep.css(STARS_SELECTOR)
            # only for ghost or private? packages
            if repo_stars_list:
                repo_stars = repo_stars_list[0].text().strip()
                repo_stars_num = int(repo_stars.replace(",", ""))
            else:
                continue

            if repo_stars_num != 0:
                more_than_zero_count += 1
            if repo_stars_num >= minstar:
                relative_repo_url = dep.css(REPO_SELECTOR)[0].attributes["href"]
                repo_url = "{0}{1}".format(GITHUB_URL, relative_repo_url)

                # can be listed same package
                is_already_added = already_added(repo_url, repos)
                if not is_already_added and repo_url != url:
                    # print("adding repo ", repo_url)
                    found_repos += 1

                    repos.append({
                        "url": repo_url,
                        "stars": repo_stars_num
                    })

                    if found_repos >= REPOS_PER_FILE_SIZE_LIMIT:
                        sorted_repos = sort_repos(repos, rows)
                        repos = []
                        number_of_files_processed += 1
                        found_repos = 0

                        show_result(
                            sorted_repos,
                            total_repos_count,
                            more_than_zero_count,
                            destinations,
                            number_of_files_processed
                        )

                        print("JSON output placed into file!")


        node = parsed_node.css(NEXT_BUTTON_SELECTOR)
        if len(node) == 2:
            page_url = node[1].attributes["href"]
        elif len(node) == 0 or node[0].text() == "Previous":
            # spinner.stop()
            break
        elif node[0].text() == "Next":
            page_url = node[0].attributes["href"]


    sorted_repos = sort_repos(repos, rows)

    if search:
        for repo in repos:
            repo_path = urlparse(repo["url"]).path[1:]
            for s in gh.search_code("{0} repo:{1}".format(search, repo_path)):
                click.echo("{0} with {1} stars".format(s.html_url, repo["stars"]))
    elif number_of_files_processed == 0:
        show_result(sorted_repos, total_repos_count, more_than_zero_count, destinations, number_of_files_processed)
