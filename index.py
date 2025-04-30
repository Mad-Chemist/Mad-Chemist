import datetime
from dateutil import relativedelta
from ascii_magic import AsciiArt
import requests
import os
from lxml import etree
import time
import hashlib
import json
from io import BytesIO
from PIL import Image
import numpy as np
import rembg

# CREDIT TO https://github.com/Andrew6rant

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
# Issues and pull requests permissions not needed at the moment, but may be used in the future
HEADERS = {'Authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}
ASCII_GEN_COLS = 60
ASCII_PRINT_COLS = 38
ASCII_MAX_LINES = 25

def load_config(file_path='config.json'):
    try:
        with open(file_path, 'r') as f:
            config = json.load(f)
        print(f"Successfully loaded config from {file_path}")
        return config
    except FileNotFoundError:
        print(f"Error: {file_path} not found")
        raise
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse {file_path}: {e}")
        raise

def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return str(diff.years)


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    print(f"Making request in {func_name}...")
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=20)  # Add timeout=20
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    time.sleep(1.0)  # Add 1-second delay
    retry_range = 5
    for attempt in range(retry_range):  # Retry up to 3 times
        print(f"Making request in recursive_loc (attempt {attempt + 1}/{retry_range})...", flush=True)
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS, timeout=20)
        if request.status_code == 200:
            if request.json()['data']['repository']['defaultBranchRef'] != None:
                return loc_counter_one_repo(owner, repo_name, data, cache_comment, request.json()['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
            else:
                return 0
        elif request.status_code in (502, 503, 504):  # Retry on gateway errors
            print(f"API request in recursive_loc failed with status {request.status_code}, attempt {attempt + 1}/{retry_range}. Retrying after delay...", flush=True)
            time.sleep(2 ** attempt)  # Exponential backoff
            continue
        elif request.status_code == 403:
            raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
        else:
            force_close_file(data, cache_comment)
            raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)
    # If all retries fail
    force_close_file(data, cache_comment)
    raise Exception('recursive_loc() failed after 3 retries with status', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time)
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+get_hash_file_name()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines

    # Verify data length matches len(edges)
    if len(data) != len(edges):
        print(f"Cache file mismatch: expected {len(edges)} entries, found {len(data)}. Reinitializing cache...", flush=True)
        flush_cache(edges, filename, comment_size)
        time.sleep(0.1)
        with open(filename, 'r') as f:
            data = f.readlines()
        cache_comment = data[:comment_size]
        data = data[comment_size:]

    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    print(f"Starting flush_cache for {len(edges)} repositories...", flush=True)
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')
    print(f"Cache file flushed with {len(edges)} entries", flush=True)

def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+get_hash_file_name()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars

def extract_html_for_ascii(html):
    avatar_rows = [[]]
    root = etree.HTML("<pre>" + html + "</pre>")
    children = root.cssselect('pre > *')
    color_re = re.compile(r'color:\s*(#[0-9a-fA-F]{6})')
    row_pos = 0
    for child in children:
        print(f"found element in extract_html_for_ascii {child.tag}")
        if child.tag == 'br':
            # Start a new row
            row_pos += 1
            avatar_rows.append([])
        elif child.tag == 'span':
            # Extract color from style attribute
            style = child.get('style', '')
            color_match = color_re.search(style)
            color = color_match.group(1)
            text = child.text
            avatar_rows[row_pos].append((text, color))

    return avatar_rows

def draw_avatar_color_ascii(root, ascii):
    start_x = 15
    start_y = 30
    line_height = 20
    avatar = root.find(f".//*[@id='avatar']")
    # Clear any existing content
    for child in avatar:
        avatar.remove(child)

    avatar_rows = extract_html_for_ascii(ascii)
    for row_idx, row in enumerate(avatar_rows):
        print(f"enumerating html ascii line {row_idx} length: {len(row)}")
        x_pos = start_x
        text_elem = etree.SubElement(avatar, "text", x=str(x_pos), y=str(start_y+(row_idx*line_height)))
        for text, color in row:
            tspan = etree.SubElement(text_elem, "tspan", style=f'fill: {color};')
            tspan.text = text if text != ' ' else '\u00A0'  # Use non-breaking space for spaces

def generate_avatar_ascii(avatar_url):
    # Download the avatar image
    response = requests.get(avatar_url, timeout=10)
    if response.status_code != 200:
        return "Failed to download avatar"

    input_image = Image.open(BytesIO(response.content))
    input_array = np.array(input_image)
    output_array = rembg.remove(input_array)
    output_image = Image.fromarray(output_array)
    output_image.save('avatar.png')

    # Convert to ASCII art
    art = AsciiArt.from_image("avatar.png")
#     ascii_text = art.to_ascii(columns=ASCII_GEN_COLS, monochrome=True)  # Adjust columns for size
    ascii_text = art.to_html(columns=ASCII_GEN_COLS, width_ratio=2)
    return ascii_text

def svg_overwrite(filename, config, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, ascii_text):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()

#     draw_avatar_ascii(root, ascii_text)
    draw_avatar_color_ascii(root, ascii_text)
    justify_format(root, 'age_data', age_data, 52)
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 7)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 8)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1])

    for custom in config['custom_values']:
        justify_format(root, custom['id'], custom['value'], custom['length'])

    tree.write(filename, encoding='utf-8', xml_declaration=True)

def draw_avatar_ascii(root, avatar_text):
    un_pad = int((ASCII_GEN_COLS-ASCII_PRINT_COLS)/2)
    start_x = 15
    start_y = 30
    line_height = 20
    ascii_art_lines = avatar_text.split('\n')
    total_lines = len(ascii_art_lines)
    total_line_offset = 0 if total_lines <= ASCII_MAX_LINES else int((total_lines-ASCII_MAX_LINES) /2)
    avatar = root.find(f".//*[@id='avatar']")
    # Clear any existing content
    for child in avatar:
        avatar.remove(child)

    # Add each line of ASCII art as a <tspan>
    for i, line in enumerate(ascii_art_lines):
        if total_line_offset <= i < ASCII_MAX_LINES+total_line_offset:
            tspan = etree.SubElement(
                avatar,
                "tspan",
                x=f'{start_x}',
                y=f'{str(int(start_y) + (i-total_line_offset) * line_height)}'
            )
            tspan.text = line[un_pad:-un_pad]


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifies the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if length == 0:
        dot_string = ''
    elif just_len == 0:
        dot_string = ' '
    elif just_len == 1:
        dot_string = ' .'
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element in the SVG file and replaces its text with a new value
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text

def get_hash_file_name():
    return hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()

def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+get_hash_file_name()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
            avatarUrl
        }
    }'''
    print(f"Fetching user data for {username}...")
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    print(f"User data fetched, status: {request.status_code}")
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt'], request.json()['data']['user']['avatarUrl']

def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date, avatar_url = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(1991, 11, 20))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    avatar_ascii, ascii_time = perf_counter(generate_avatar_ascii, avatar_url)

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    config = load_config('config.json')
    svg_overwrite('dark_mode.svg', config, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], avatar_ascii)
    svg_overwrite('light_mode.svg', config, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], avatar_ascii)

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
