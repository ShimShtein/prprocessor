import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Collection, Dict, Generator, Iterable, Mapping, Optional, Tuple

import yaml
from octomachinery.app.routing import process_event_actions
from octomachinery.app.routing.decorators import process_webhook_payload
from octomachinery.app.runtime.context import RUNTIME_CONTEXT
from octomachinery.app.server.runner import run as run_app
from pkg_resources import resource_filename
from redminelib.resources import Issue, Project

from prprocessor.compat import strip_suffix
from prprocessor.redmine import (Field, Status, get_issues, get_latest_open_version, get_redmine,
                                 set_fixed_in_version, verify_issues, IssueValidation)


COMMIT_VALID_SUMMARY_REGEX = re.compile(
    r'\A(?P<action>fixes|refs) (?P<issues>#(\d+)(, ?#(\d+))*)(:| -) .*\Z',
    re.IGNORECASE,
)
COMMIT_ISSUES_REGEX = re.compile(r'#(\d+)')
CHECK_NAME = 'Redmine issues'
WHITELISTED_ORGANIZATIONS = ('theforeman', 'Katello')


class UnconfiguredRepository(Exception):
    pass


@dataclass
class Commit:
    sha: str
    message: str
    fixes: set = field(default_factory=set)
    refs: set = field(default_factory=set)

    @property
    def subject(self):
        return self.message.splitlines()[0]


@dataclass
class Config:
    project: Optional[str] = None
    required: bool = False
    refs: set = field(default_factory=set)
    version_prefix: Optional[str] = None


# This should be handled cleaner
with open(resource_filename(__name__, 'config/repos.yaml')) as config_fp:
    CONFIG = {
        repo: Config(project=config.get('redmine'), required=config.get('redmine_required', False),
                     refs=set(config.get('refs', [])),
                     version_prefix=config.get('redmine_version_prefix'))
        for repo, config in yaml.safe_load(config_fp).items()
    }

with open(resource_filename(__name__, 'config/users.yaml')) as users_fp:
    USERS = yaml.safe_load(users_fp)


logger = logging.getLogger('prprocessor')  # pylint: disable=invalid-name


def get_config(repository: str) -> Config:
    try:
        return CONFIG[repository]
    except KeyError:
        user, _ = repository.split('/', 1)
        if user not in WHITELISTED_ORGANIZATIONS:
            logger.info('The repository %s is unconfigured and user %s not whitelisted',
                        repository, user)
            raise UnconfiguredRepository(f'The repository {repository} is unconfigured')
        return Config()


def pr_is_cherry_pick(pull_request: Mapping) -> bool:
    return pull_request['title'].startswith(('CP', '[CP]', 'Cherry picks for '))


def summarize(summary: Mapping[str, Iterable], show_headers: bool) -> Generator[str, None, None]:
    for header, lines in summary.items():
        if lines:
            if show_headers:
                yield f'### {header}'
            for line in lines:
                yield f'* {line}'


async def get_commits_from_pull_request(pull_request: Mapping) -> AsyncGenerator[Commit, None]:
    github_api = RUNTIME_CONTEXT.app_installation_client
    items = await github_api.getitem(pull_request['commits_url'])
    for item in items:
        commit = Commit(item['sha'], item['commit']['message'])

        match = COMMIT_VALID_SUMMARY_REGEX.match(commit.subject)
        if match:
            action = getattr(commit, match.group('action').lower())
            for issue in COMMIT_ISSUES_REGEX.findall(match.group('issues')):
                action.add(int(issue))

        yield commit


async def set_check_in_progress(pull_request: Mapping, check_run=None):
    github_api = RUNTIME_CONTEXT.app_installation_client

    data = {
        'name': CHECK_NAME,
        'head_branch': pull_request['head']['ref'],
        'head_sha': pull_request['head']['sha'],
        'status': 'in_progress',
        'started_at': datetime.now(tz=timezone.utc).isoformat(),
    }

    if check_run:
        if check_run['status'] != 'in_progress':
            await github_api.patch(check_run['url'], data=data, preview_api_version='antiope')
    else:
        url = f'{pull_request["base"]["repo"]["url"]}/check-runs'
        check_run = await github_api.post(url, data=data, preview_api_version='antiope')

    return check_run


def format_invalid_commit_messages(commits: Iterable[Commit]) -> Collection[str]:
    return [f"{commit.sha} must be in the format `fixes #redmine - brief description`"
            for commit in commits]


def format_redmine_issues(issues: Iterable[Issue]) -> Collection[str]:
    return [f"[#{issue.id}: {issue.subject}]({issue.url})"
            for issue in sorted(issues, key=lambda issue: issue.id)]


def format_details(invalid_issues: Iterable[Issue], correct_project: Project) -> str:
    text = []
    for issue in invalid_issues:
        # Would be nice to get the new issue URL via a property
        text.append(f"""### [#{issue.id}: {issue.subject}]({issue.url})

* check [#{issue.id}]({issue.url}) is the intended issue
* move [ticket #{issue.id}]({issue.url}) from {issue.project.name} to the {correct_project.name} project
* or file a new ticket in the [{correct_project.name} project]({correct_project.url}/issues/new)
""")

    return '\n'.join(text)


async def get_issues_from_pr(pull_request: Mapping) -> Tuple[IssueValidation, Collection]:
    config = get_config(pull_request['base']['repo']['full_name'])

    issue_ids = set()
    invalid_commits = []

    async for commit in get_commits_from_pull_request(pull_request):
        issue_ids.update(commit.fixes)
        issue_ids.update(commit.refs)
        if config.required and not commit.fixes and not commit.refs:
            invalid_commits.append(commit)

    return verify_issues(config, issue_ids), invalid_commits


async def run_pull_request_check(pull_request: Mapping, check_run=None) -> None:
    github_api = RUNTIME_CONTEXT.app_installation_client

    check_run = await set_check_in_progress(pull_request, check_run)

    # We're very pessimistic
    conclusion = 'failure'

    attempts = 3

    try:
        for attempt in range(1, attempts + 1):
            try:
                issue_results, invalid_commits = await get_issues_from_pr(pull_request)
                break
            except:  # pylint: disable=bare-except
                if attempt == attempts:
                    raise
                logger.exception('Failure during validation of PR (attempt %s)', attempt)
                await asyncio.sleep(attempt)
    except UnconfiguredRepository:
        output = {
            'title': 'Unknown repository',
            'summary': 'Contact us via [Discourse](https://community.theforeman.org]',
        }
    except:  # pylint: disable=bare-except
        logger.exception('Failure during validation of PR')
        output = {
            'title': 'Internal error while testing',
            'summary': 'Please retry later',
        }
    else:
        try:
            await update_redmine_on_issues(pull_request, issue_results.valid_issues)
        except:  # pylint: disable=bare-except
            logger.exception('Failed to update Redmine issues')

        summary: Dict[str, Collection] = {
            'Invalid commits': format_invalid_commit_messages(invalid_commits),
            'Invalid project': format_redmine_issues(issue_results.invalid_project_issues),
            'Issues not found in redmine': issue_results.missing_issue_ids,
            'Valid issues': format_redmine_issues(issue_results.valid_issues),
        }

        non_empty = [title for title, lines in summary.items() if lines]
        multiple_sections = len(non_empty) != 1
        if not any(True for header in non_empty if header != 'Valid issues'):
            conclusion = 'success'

        output = {
            'title': 'Redmine Issue Report' if multiple_sections else non_empty[0],
            'summary': '\n'.join(summarize(summary, multiple_sections)),
            'text': format_details(issue_results.invalid_project_issues, issue_results.project),
        }

        # > For 'properties/text', nil is not a string.
        # That means it's not possible to delete the text by setting None, but
        # sometimes we can avoid setting it
        if not output['text'] and not check_run['output'].get('text'):
            del output['text']

    await github_api.patch(
        check_run['url'],
        preview_api_version='antiope',
        data={
            'status': 'completed',
            'head_branch': pull_request['head']['ref'],
            'head_sha': pull_request['head']['sha'],
            'completed_at': datetime.now(tz=timezone.utc).isoformat(),
            'conclusion': conclusion,
            'output': output,
        },
    )


async def update_redmine_on_issues(pull_request: Mapping, issues: Iterable[Issue]) -> None:
    pr_url = pull_request['html_url']
    assignee = USERS.get(pull_request['user']['login'])

    for issue in issues:
        status = Status(issue.status.id)

        if not status.is_rejected():
            updates = {}
            # TODO: rewrite this
            #if issue.backlog or issue.recycle_bin or not issue.fixed_version_id:
            #    triaged_field = issue.custom_fields.get(Field.TRIAGED)
            #    if triaged_field.value is True:  # TODO does the API return a boolean?
            #        updates['custom_fields'] = [{'id': triaged_field.id, 'value': False}]

            #    updates['fixed_version_id'] = None

            if not pr_is_cherry_pick(pull_request):
                pr_field = issue.custom_fields.get(Field.PULL_REQUEST)
                if pr_url not in pr_field.value:
                    if 'custom_fields' not in updates:
                        updates['custom_fields'] = []
                    new_value = pr_field.value + [pr_url]
                    updates['custom_fields'].append({'id': pr_field.id, 'value': new_value})

            if assignee and not hasattr(issue, 'assigned_to'):
                updates['assigned_to_id'] = assignee

            if not (status.is_closed() or status == Status.READY_FOR_TESTING):
                updates['status_id'] = Status.READY_FOR_TESTING.value

            if updates:
                logger.info('Updating issue %s: %s', issue.id, updates)
                issue.save(**updates)
            else:
                logger.debug('Redmine issue %s already in sync', issue.id)


@process_event_actions('pull_request', {'opened', 'ready_for_review', 'reopened', 'synchronize'})
@process_webhook_payload
async def on_pr_modified(*, pull_request: Mapping, **other) -> None:  # pylint: disable=unused-argument
    await run_pull_request_check(pull_request)


@process_event_actions('check_run', {'rerequested'})
@process_webhook_payload
async def on_check_run(*, check_run: Mapping, **other) -> None:  # pylint: disable=unused-argument
    github_api = RUNTIME_CONTEXT.app_installation_client

    if not check_run['pull_requests']:
        logger.warning('Received check_run without PRs')

    for pr_summary in check_run['pull_requests']:
        pull_request = await github_api.getitem(pr_summary['url'])
        await run_pull_request_check(pull_request, check_run)


@process_event_actions('check_suite', {'requested', 'rerequested'})
@process_webhook_payload
async def on_suite_run(*, check_suite: Mapping, **other) -> None:  # pylint: disable=unused-argument
    github_api = RUNTIME_CONTEXT.app_installation_client

    check_runs = await github_api.getitem(check_suite['check_runs_url'],
                                          preview_api_version='antiope')

    for check_run in check_runs['check_runs']:
        if check_run['name'] == CHECK_NAME:
            break
    else:
        check_run = None

    if not check_suite['pull_requests']:
        logger.warning('Received check_suite without PRs')

    for pr_summary in check_suite['pull_requests']:
        pull_request = await github_api.getitem(pr_summary['url'])
        await run_pull_request_check(pull_request, check_run)


@process_event_actions('pull_request', {'closed'})
@process_webhook_payload
async def on_pr_merge(*, pull_request: Mapping, **other) -> None:  # pylint: disable=unused-argument
    """
    Only acts on merged PRs to a master or develop branch. There is no handling for stable
    branches.

    If there's a configuration, all related issues that have a Fixes #xyz are gathered. All of
    those that have a matching project according to the configuration are considered. With that
    list, the Redmine project's latest version is determined. If there is one, all issues receive
    the fixed_in_version.
    """

    if not pull_request['merged']:
        logger.debug('Pull request %s was closed, not merged', pull_request['number'])
        return

    repository = pull_request['base']['repo']['full_name']
    target_branch = pull_request['base']['ref']
    if target_branch.endswith('-stable'):
        # Handle a branch like 3.0-stable. This means we get an additional prefix of 3.0. which
        # allows get_latest_open_version to find the right version
        version_prefix = f'{strip_suffix(target_branch, "-stable")}.'
    elif target_branch in ('main', 'master', 'develop', 'deb/develop', 'rpm/develop'):
        # Development branches don't have a version prefix so they really use the latest
        version_prefix = ''
    else:
        logger.info('Unable to set fixed in version for %s branch %s in PR %s',
                    repository, target_branch, pull_request['number'])
        return

    try:
        config = get_config(repository)
    except UnconfiguredRepository:
        return
    else:
        if not config.project:
            logger.info('Repository for %s not found', repository)
            return
        if config.version_prefix:
            version_prefix = f'{config.version_prefix}{version_prefix}'

    issue_ids = set()
    async for commit in get_commits_from_pull_request(pull_request):
        issue_ids.update(commit.fixes)

    if issue_ids:
        redmine = get_redmine()
        project = redmine.project.get(config.project)
        fixed_in_version = get_latest_open_version(project, version_prefix)

        if not fixed_in_version:
            logger.info('Unable to determine latest version for %s; prefix=%s', project.name,
                        version_prefix)
            return

        for issue in get_issues(redmine, issue_ids):
            if issue.project.id == project.id:
                logger.info('Setting fixed in version for issue %s to %s', issue.id,
                            fixed_in_version.name)
                set_fixed_in_version(issue, fixed_in_version)


def run_prprocessor_app() -> None:
    run_app(
        name='prprocessor',
        version='0.1.0',
        url='https://github.com/apps/prprocessor',
    )


if __name__ == "__main__":
    run_prprocessor_app()
