from datetime import time
import json
import re
from pathlib import Path
from pprint import pprint
from sys import stdout
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup
import requests
from tqdm import tqdm

from modules.file_manipulation import write_logs

postprocess_folder = "./postprocess"


THROTTLE_TIME = 24


# twitter_auth = tweepy.OAuth1UserHandler(
#     consumer_key,
#     consumer_secret,
#     access_token,
#     access_token_secret
# )
def twitter_postprocess(artist, url):
    pass


def fetch(api_url, empty_response):
    # print("\n\n\napi_url: " + api_url +"\n\n\n")
    try:
        while True:
            try:
                res = requests.get(api_url, headers={"accept": "application/json"})
            except:
                if res.status_code == 429 or res.status_code == 403:
                    time.sleep(THROTTLE_TIME)
                else:
                    break
            else:
                break
    except:
        # print("ERROR: " + api_url)
        return empty_response, res

    # print("Correct: " + api_url)
    if res.status_code != 200:
        # stdout.write(f"[PostProcess] ERROR: Failed to fetch using API ({api_url})\n")
        # stdout.write(f"[PostProcess] ERROR: Status code: {res.status_code}\n")
        return empty_response, res

    # print("\n\n\n")
    # print(res.json())
    # print("\n\n\n")
    return res.json(), res


class KemonoPostProcess:
    _KEMONO_SERVER = "https://kemono.cr/api"

    _CREATOR_POSTS = "/v1/{service}/user/{creator_id}"
    _CREATOR_POST_BY_ID = "/v1/{service}/user/{creator_id}/post/{post_id}"

    _POSTS_PER_FETCH = 50

    artist = ""
    base_url = ""
    logfile = ""

    @classmethod
    def process_page(self, artist, url, depth, logfile):
        url_section = url.split("/")
        base_url = "kemono" in url and self._KEMONO_SERVER or "Coomer"
        offset = 0

        self.artist = artist
        self.base_url = base_url
        self.logfile = logfile

        pbar = tqdm(
            desc="Searching Posts",
            total=0,
            delay=1.5,
            position=depth,
            dynamic_ncols=True,
            ncols=80,
        )

        all_posts = []
        # pbar.write(
        #     f"[PostProcess] INFO: Fetching posts from {artist}'s {url_section[-3]} page."
        # )
        while True:

            curr_posts, res = self.fetch_posts(
                base_url, url_section[-3], url_section[-1], offset=offset
            )
            # pprint(f"\n\n\n{curr_posts}\n\n\n")
            all_posts = all_posts + curr_posts
            offset += len(curr_posts)
            pbar.total = offset
            pbar.refresh()
            if len(curr_posts) % self._POSTS_PER_FETCH != 0 or len(curr_posts) == 0:
                break
        # pbar.write(f"[PostProcess] INFO: Parsing media from the {len(all_posts)} posts")
        pbar.clear()
        post_content = self.parse_posts(base_url, all_posts, depth)
        user_name = json.loads(requests.request(
            "GET",
            f"{base_url}{self._CREATOR_POSTS.format(service=url_section[-3], creator_id=url_section[-1])}/profile",
            headers={"Accept": "text/css"}
        ).text)["name"]
        self.link_extraction_from_posts(
            post_content=post_content,
            filename=f"{artist}'s {url_section[-3]} ({user_name}) from {("kemono" in base_url and "Kemono" or "Coomer")}",
            depth=depth,
        )

    @classmethod
    def fetch_posts(self, base_url, service, user, offset):
        api_url = base_url + self._CREATOR_POSTS.format(
            service=service, creator_id=user
        )

        if offset is not None:
            api_url = f"{api_url}?o={offset}"
        return fetch(api_url=api_url, empty_response=[])

    # @classmethod
    # def post_parsing(self, post):
    #     api_url = self.base_url + self._CREATOR_POST_BY_ID.format(
    #         service=post["service"], creator_id=post["user"], post_id=post["id"]
    #     )
    #     curr_post = fetch(api_url=api_url, empty_response={})

    #     if curr_post == {}:
    #         return (post["post"]["title"], "Error!")
    #     else:
    #         # stdout.write(str(curr_post))
    #         return (curr_post["post"]["title"], curr_post["post"]["content"])

    @classmethod
    def parse_posts(self, base_url, all_posts, depth):
        # pbar = tqdm(
        #     all_posts,
        #     desc="Fetching posts",
        #     delay=1.5,
        #     position=depth,
        #     dynamic_ncols=True,
        #     ncols=80,
        # )
        # post_content = start_threads(thread_nb=10, work_method=self.post_parsing, args=all_posts, bar=pbar)
        post_content = []
        for post in tqdm(
            all_posts,
            desc="Fetching posts",
            delay=1.5,
            position=depth,
            dynamic_ncols=True,
            ncols=80,
        ):
            api_url = base_url + self._CREATOR_POST_BY_ID.format(
                service=post["service"], creator_id=post["user"], post_id=post["id"]
            )
            write_logs(self.logfile, f"{api_url}", with_line_break=True)
            curr_post, res = fetch(api_url=api_url, empty_response={})

            if curr_post == {}:
                try:
                    write_logs(self.logfile, f"\tError: {res}", with_line_break=False)
                    post_content.append((post["post"]["title"], "Error!"))
                except KeyError:
                    post_content.append((post["title"], post["content"]))
                    write_logs(self.logfile, f"\tDone.", with_line_break=False)
            else:
                # stdout.write(str(curr_post))
                post_content.append(
                    (curr_post["post"]["title"], curr_post["post"]["content"])
                )
                write_logs(self.logfile, f"\tDone.", with_line_break=False)
        return post_content

    @classmethod
    def link_extraction_from_posts(self, filename, post_content, depth):
        with open(f"{postprocess_folder}/{filename}.txt", "w", encoding="utf-8") as file:
            pbar = tqdm(
                post_content,
                desc=f"{self.artist}'s {filename[-6:]}",
                delay=1.5,
                position=depth,
                dynamic_ncols=True,
                ncols=80,
            )
            has_error = False
            for title, post in post_content:
                file.write(f"{"-" * 10}{title}{"-" * 10}\n")
                if "".startswith("Error"):
                    has_error = True
                    file.write(f"An error has occurred during the fetch of this page\n")
                    continue

                hyperlink = re.compile(r'https?://[^\s\'"<>#]+(?:#[^\s\'"<>]*)?')
                for i in list(
                    set(re.findall(hyperlink, re.sub("</(.*?)>|&nbsp;", "", post)))
                ):
                    file.write(f"{i}\n")
                pbar.update()

            file.write(f"{"-" * 15}END OF FILE{"-" * 15}\n")
            pbar.desc = (has_error and "\U0001f7e2" or "\U0001f534") + pbar.desc


# def kemono_postprocess(artist, url, depth):

#     path = Path(postprocess_folder) / artist
#     path.mkdir(parents=True, exist_ok=True)
#     path = path / f"{re.sub(r"[/:]", " ", url.replace("https://", ""))}.txt"
#     # if path.exists():
#     #     return
#     response = requests.get(url)
#     url_pattern = re.compile(f'href="{url.replace("https://kemono.su", "")}\\?o=\\d+"')
#     content = ""
#     html_content = response.text
#     soup = BeautifulSoup(html_content, "html.parser")

#     title_tag = soup.find("title").text.strip()

#     print(title_tag)

#     exit(0)

#     post_per_page = 50
#     pages = []
#     max_page = 0
#     for link in url_pattern.findall(content):
#         i = int(re.findall(r"\d+", parse_qs(urlparse(link).query)["o"][0])[0])
#         max_page = i if i > max_page else max_page
#     print("maw page: " + str(max_page))
#     for i in range(1, int(max_page / post_per_page) + 1):
#         if (i * post_per_page) not in pages:
#             pages.append(i * post_per_page)

#     print("pages: " + str(pages))

#     pbar = tqdm(
#         desc="Searching Posts",
#         total=0,
#         delay=1.5,
#         position=depth,
#         dynamic_ncols=True,
#         ncols=80,
#     )
#     url_pattern = re.compile(f'href="{url.replace("https://kemono.su", "")}/post/\\d+"')
#     print(url_pattern.findall(content))
#     posts = []
#     input("")
#     for post in url_pattern.findall(content):
#         print(post)
#         posts.append(re.findall(r"/post/\d+", post)[0][6:])
#         pbar.total += 1
#         pbar.refresh()

#     for page in tqdm(pages, desc="Pages"):
#         response = requests.get(f"{url}?o={page}")
#         for post in url_pattern.findall(response.text):
#             posts.append(re.findall(r"/post/\d+", post)[0][6:])
#             pbar.total += 1
#             pbar.refresh()

#     links = []
#     hyperlink = re.compile(r'https?://[^\s\'"<>#]+(?:#[^\s\'"<>]*)?')
#     print("posts: " + str(posts))
#     input()

#     for post in tqdm(
#         posts, desc="Posts", delay=1.5, position=depth, dynamic_ncols=True, ncols=80
#     ):
#         response = requests.get(f"{url}/post/{post}")
#         links.append("━" * 100)
#         links.append(f"{url}/post/{post}")
#         links.append("*" * 50)
#         for i in list(set(re.findall(hyperlink, response.text))):
#             if i != f"{url}/post/{post}":
#                 links.append(i)

#     wanted_links = []
#     for link in links:
#         if not link.startswith(
#             (
#                 "https://n1.kemono.su/data",
#                 "https://n2.kemono.su/data",
#                 "https://n3.kemono.su/data",
#                 "https://n4.kemono.su/data",
#                 "https://chan.kemono.party",
#                 "https://status.kemono.su",
#                 "https://kemono.su/matrix",
#                 "https://theporndude.com",
#                 "https://ogp.me/ns#",
#                 "https://go.mnaspm.com",
#             )
#         ):
#             wanted_links.append(link)

#     with open(
#         f"{postprocess_folder}/{artist}/{re.sub(r"[/:]", " ", url.replace("https://", ""))}.txt",
#         "w",
#         encoding="utf-8",
#     ) as f:
#         for link in wanted_links:
#             f.write("%s\n" % link)


if __name__ == "__main__":
    KemonoPostProcess().process_page(
        # "Zex_art", "https://kemono.cr/patreon/user/409001", 0
        "K_Bloodstein", "https://kemono.su/patreon/user/81070127", 0, "test.log"
    )
    # kemono_postprocess("melonart", "https://kemono.su/patreon/user/12464517", 0)
