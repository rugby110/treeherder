import logging

import newrelic.agent
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from treeherder.etl.common import (fetch_json,
                                   to_timestamp)
from treeherder.model.derived.jobs import JobsModel
from treeherder.model.models import Repository

logger = logging.getLogger(__name__)


class ResultsetLoader:
    """Transform and load a list of Resultsets"""

    def process(self, message_body, exchange):
        try:
            transformer = self.get_transformer_class(exchange)(message_body)
            repo = Repository.objects.get(url=transformer.repo_url,
                                          branch=transformer.branch,
                                          active_status="active")
            transformed_data = transformer.transform(repo.name)

            with JobsModel(repo.name) as jobs_model:
                logger.info("Storing resultset for {} {}".format(
                    repo.name,
                    transformer.repo_url))
                jobs_model.store_result_set_data([transformed_data])

        except ObjectDoesNotExist:
            newrelic.agent.record_custom_event("skip_unknown_repository",
                                               transformer.repo_url)
            logger.warn("Skipping unsupported repo: {}".format(
                transformer.repo_url))
        except Exception as ex:
            newrelic.agent.record_exception(exc=ex)
            logger.exception("Error transforming resultset", exc_info=ex)

    def get_transformer_class(self, exchange):
        if "github" in exchange:
            if exchange.endswith("push"):
                return GithubPushTransformer
            elif exchange.endswith("pull-request"):
                return GithubPullRequestTransformer
        elif "hgpushes" in exchange:
            return HgPushTransformer
        raise PulseResultsetError(
            "Unsupported resultset exchange: {}".format(exchange))


class GithubTransformer:

    CREDENTIALS = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "client_secret": settings.GITHUB_CLIENT_SECRET
    }

    def __init__(self, message_body):
        self.message_body = message_body
        self.repo_url = message_body["details"]["event.head.repo.url"].replace(
                ".git", "")
        self.branch = self.get_branch()

    def get_branch(self):
        return self.message_body["details"]["event.base.repo.branch"]

    def fetch_resultset(self, url, repository, sha=None):
        params = {"sha": sha} if sha else {}
        params.update(self.CREDENTIALS)

        logger.info("Fetching resultset details: {}".format(url))
        try:
            commits = self.get_cleaned_commits(fetch_json(url, params))
            head_commit = commits[-1]
            resultset = {
                "revision": head_commit["sha"],
                "push_timestamp": to_timestamp(
                    head_commit["commit"]["author"]["date"]),
                "author": head_commit["commit"]["author"]["email"],
            }

            revisions = []
            for commit in commits:
                revisions.append({
                    "comment": commit["commit"]["message"],
                    "repository": repository,
                    "author": "{} <{}>".format(
                        commit["commit"]["author"]["name"],
                        commit["commit"]["author"]["email"]),
                    "revision": commit["sha"]
                })

            resultset["revisions"] = revisions
            return resultset

        except Exception as ex:
            logger.exception("Error fetching commits", exc_info=ex)
            newrelic.agent.record_exception(ex, params={
                "url": url, "repository": repository, "sha": sha
                })

    def get_cleaned_commits(self, commits):
        """Allow a subclass to change the order of the commits"""
        return commits


class GithubPushTransformer(GithubTransformer):
    # {
    #     organization:mozilla - services
    #     details:{
    #         event.type:push
    #         event.base.repo.branch:master
    #         event.head.repo.branch:master
    #         event.head.user.login:mozilla-cloudops-deploy
    #         event.head.repo.url:https://github.com/mozilla-services/cloudops-jenkins.git
    #         event.head.sha:845aa1c93726af92accd9b748ea361a37d5238b6
    #         event.head.ref:refs/heads/master
    #         event.head.user.email:mozilla-cloudops-deploy@noreply.github.com
    #     }
    #     repository:cloudops-jenkins
    #     version:1
    # }

    URL_BASE = "https://api.github.com/repos/{}/{}/commits"

    def transform(self, repository):
        commit = self.message_body["details"]["event.head.sha"]
        push_url = self.URL_BASE.format(
            self.message_body["organization"],
            self.message_body["repository"]
        )
        return self.fetch_resultset(push_url, repository, sha=commit)

    def get_cleaned_commits(self, commits):
        # The list of commits will include ones not in the push.  we
        # need to trim the list
        base_sha = self.message_body["details"]["event.base.sha"]
        for idx, commit in enumerate(commits):
            if commit["sha"] == base_sha:
                commits = commits[:idx]
        return list(reversed(commits))


class GithubPullRequestTransformer(GithubTransformer):
    # {
    #     "organization": "mozilla",
    #     "action": "synchronize",
    #     "details": {
    #         "event.type": "pull_request.synchronize",
    #         "event.base.repo.branch": "master",
    #         "event.pullNumber": "1692",
    #         "event.base.user.login": "mozilla",
    #         "event.base.repo.url": "https: // github.com / mozilla / treeherder.git",
    #         "event.base.sha": "ff6a66a27c2c234e5820b8ffe48f17d85f1eb2db",
    #         "event.base.ref": "master",
    #         "event.head.user.login": "mozilla",
    #         "event.head.repo.url": "https: // github.com / mozilla / treeherder.git",
    #         "event.head.repo.branch": "github - pulse - resultsets",
    #         "event.head.sha": "0efea0fa1396369b5058e16139a8ab51cdd7bd29",
    #         "event.head.ref": "github - pulse - resultsets",
    #         "event.head.user.email": "mozilla@noreply.github.com",
    #     },
    #     "repository": "treeherder",
    #     "version": 1
    # }

    URL_BASE = "https://api.github.com/repos/{}/{}/pulls/{}/commits"

    def get_branch(self):
        """
        Pull requests don't use the actual branch, just the string "pull request"
        """
        return "pull request"

    def transform(self, repository):
        pr_url = self.URL_BASE.format(
            self.message_body["organization"],
            self.message_body["repository"],
            self.message_body["details"]["event.pullNumber"]
        )

        return self.fetch_resultset(pr_url, repository)


class HgPushTransformer:
    """
    {
      "root": {
        "payload": {
          "pushlog_pushes": [
            {
              "time": 14698302460,
              "push_full_json_url": "https://hg.mozilla.org/try/json-pushes?version=2&full=1&startID=136597&endID=136598",
              "pushid": 136598,
              "push_json_url": " https: //hg.mozilla.org/try/json-pushes?version=2&startID=136597&endID=136598",
              "user": " james@hoppipolla.co.uk"
            }
          ],
          "heads": [
            "2f77bc4f354d9ba67ea5270b2fc789f4b0521287"
          ],
          "repo_url": "https://hg.mozilla.org/try",
          "_meta": {
            "sent": "2016-07-29T22:11:18.503365",
            "routing_key": "try",
            "serializer": "json",
            "exchange": "exchange/hgpushes/v1"
          }
        }
      }
    }
    """
    def __init__(self, message_body):
        self.message_body = message_body
        self.repo_url = message_body["payload"]["repo_url"]
        self.branch = None

    def transform(self, repository):
        logger.info("transforming for {}".format(repository))
        url = self.message_body["payload"]["pushlog_pushes"][0]["push_full_json_url"]
        return self.fetch_resultset(url, repository)

    def fetch_resultset(self, url, repository, sha=None):
        try:
            logger.info("fetching for {} {}".format(repository, url))
            # there will only ever be one, with this url
            push = fetch_json(url)["pushes"].values()[0]
            commits = list(push["changesets"])
            head_commit = commits[0]
            logger.info("got head commit: {}".format(head_commit["node"]))
            resultset = {
                "revision": head_commit["node"],
                "push_timestamp": push["date"],
                "author": push["user"],
            }

            revisions = []
            for commit in commits:
                revisions.append({
                    "comment": commit["desc"],
                    "repository": repository,
                    "author": commit["author"],
                    "revision": commit["node"]
                })

            resultset["revisions"] = revisions
            return resultset

        except Exception as ex:
            logger.exception("Error fetching commits", exc_info=ex)
            newrelic.agent.record_exception(ex, params={
                "url": url, "repository": repository, "sha": sha
                })


class PulseResultsetError(ValueError):
    pass
