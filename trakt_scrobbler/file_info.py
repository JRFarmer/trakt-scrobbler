import re
from functools import lru_cache
from typing import Union
from urllib.parse import unquote, urlsplit, urlunsplit

import confuse
import guessit
from trakt_scrobbler import config, logger
from trakt_scrobbler.notifier import notify
from trakt_scrobbler.utils import RegexPat, cleanup_encoding, is_url
from urlmatch import BadMatchPattern, urlmatch

cfg = config["fileinfo"]
whitelist = cfg["whitelist"].get(confuse.StrSeq())
regexes = cfg['include_regexes'].get()
exclude_patterns = cfg["exclude_patterns"].get(confuse.Sequence(RegexPat()))
use_regex = any(regexes.values())


def matches_url(path, file_path) -> bool:
    try:
        if urlmatch(path, file_path, path_required=False, fuzzy_scheme=True):
            return True
    except BadMatchPattern:
        return False
    return False


def whitelist_file(file_path: str, is_url=False, return_path=False) -> Union[bool, str]:
    """Check if the played media file is in the allowed list of paths.

    Simply checks that some whitelist path should be prefix of file_path.

    An edge case that is deliberately not handled:
    Suppose user has whitelisted "path/to/tv" directory
    and the user also has another directory "path/to/tv shows".
    If the user plays something from the latter, it will still be whitelisted.
    """
    if not whitelist:
        return True
    for path in whitelist:
        if is_url and matches_url(path, file_path) or file_path.startswith(path):
            logger.debug(f"Matched whitelist entry '{path}'")
            return path if return_path else True

    return False


def exclude_file(file_path: str) -> bool:
    for pattern in exclude_patterns:
        if pattern.match(file_path):
            logger.debug(f"Matched exclude pattern '{pattern}'")
            return True
    return False


def custom_regex(file_path: str):
    for item_type, patterns in regexes.items():
        for pattern in patterns:
            m = re.match(pattern, file_path)
            if m:
                logger.debug(f"Matched regex pattern '{pattern}'")
                guess = m.groupdict()
                guess['type'] = item_type
                return guess


def use_guessit(file_path: str):
    try:
        return guessit.guessit(file_path)
    except guessit.api.GuessitException:
        logger.exception("Encountered guessit error.")
        notify("Encountered guessit error. File a bug report!", category="exception")
        return {}


@lru_cache(maxsize=None)
def get_media_info(file_path: str):
    logger.debug(f"Raw filepath {file_path!r}")
    file_path = cleanup_encoding(file_path)
    parsed = urlsplit(file_path)
    file_is_url = False
    guessit_path = file_path
    if is_url(parsed):
        file_is_url = True
        # remove the query and fragment from the url, keeping only important parts
        scheme, netloc, path, _, _ = parsed
        path = unquote(path)  # quoting should only be applied to the path
        file_path = urlunsplit((scheme, netloc, path, "", ""))
        logger.debug(f"Converted to url {file_path!r}")
        # only use the actual path for guessit, skipping other parts
        guessit_path = path
        logger.debug(f"Guessit url {guessit_path!r}")

    if not whitelist_file(file_path, file_is_url):
        logger.info("File path not in whitelist.")
        return None
    if exclude_file(file_path):
        logger.info("Ignoring file.")
        return None
    guess = use_regex and custom_regex(file_path) or use_guessit(guessit_path)
    logger.debug(f"Guess: {guess}")
    return cleanup_guess(guess)


def cleanup_guess(guess):
    if not guess:
        return None

    if any(key not in guess for key in ('title', 'type')) or \
       (guess['type'] == 'episode' and 'episode' not in guess):
        logger.warning('Failed to parse filename for episode/movie info. '
                       'Consider renaming/using custom regex.')
        return None

    if isinstance(guess['title'], list):
        guess['title'] = " ".join(guess['title'])

    req_keys = ['type', 'title']
    if guess['type'] == 'episode':
        season = guess.get('season')
        if season is None:
            # if we don't find a season, default to 1
            season = 1  # TODO: Add proper support for absolute-numbered episodes
        if isinstance(season, list):
            logger.warning(f"Multiple probable seasons found: ({','.join(season)}). "
                           "Consider renaming the folder.")
            return None
        guess['season'] = int(season)
        req_keys += ['season', 'episode']

    if 'year' in guess:
        req_keys += ['year']

    return {key: guess[key] for key in req_keys}
