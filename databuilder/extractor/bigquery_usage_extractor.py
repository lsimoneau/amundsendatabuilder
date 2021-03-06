from collections import namedtuple
from datetime import date, timedelta
import json
import logging
import re
from time import sleep

import google.oauth2.service_account
import google_auth_httplib2
from googleapiclient.discovery import build
import httplib2
from pyhocon import ConfigTree  # noqa: F401
from typing import Dict, Optional  # noqa: F401

from databuilder.extractor.base_extractor import Extractor

TableColumnUsageTuple = namedtuple('TableColumnUsageTuple', ['database', 'cluster', 'schema',
                                                             'table', 'column', 'email'])

LOGGER = logging.getLogger(__name__)


class BigQueryTableUsageExtractor(Extractor):
    """
    An aggregate extractor for bigquery table usage. This class takes the data from
    the stackdriver logging API by filtering on timestamp, bigquery_resource and looking
    for referencedTables in the response.
    """
    TIMESTAMP_KEY = 'timestamp'
    PROJECT_ID_KEY = 'project_id'
    DEFAULT_PAGE_SIZE = 300
    PAGE_SIZE_KEY = 'page_size'
    KEY_PATH_KEY = 'key_path'
    # sometimes we don't have a key path, but only have an variable
    CRED_KEY = 'project_cred'
    _DEFAULT_SCOPES = ('https://www.googleapis.com/auth/cloud-platform',)
    EMAIL_PATTERN = 'email_pattern'
    NUM_RETRIES = 3
    DELAY_TIME = 10

    def init(self, conf):
        # type: (ConfigTree) -> None
        self.key_path = conf.get_string(BigQueryTableUsageExtractor.KEY_PATH_KEY, None)
        self.cred_key = conf.get_string(BigQueryTableUsageExtractor.CRED_KEY, None)
        if self.key_path:
            credentials = (
                google.oauth2.service_account.Credentials.from_service_account_file(
                    self.key_path, scopes=BigQueryTableUsageExtractor._DEFAULT_SCOPES))
        elif self.cred_key:
            service_account_info = json.loads(self.cred_key)
            credentials = (
                google.oauth2.service_account.Credentials.from_service_account_info(
                    service_account_info, scopes=BigQueryTableUsageExtractor._DEFAULT_SCOPES))
        else:
            credentials, _ = google.auth.default(scopes=BigQueryTableUsageExtractor._DEFAULT_SCOPES)

        http = httplib2.Http()
        authed_http = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
        self.logging_service = build('logging', 'v2', http=authed_http, cache_discovery=False)

        self.timestamp = conf.get_string(
            BigQueryTableUsageExtractor.TIMESTAMP_KEY,
            (date.today() - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z'))
        self.projectid = conf.get_string(BigQueryTableUsageExtractor.PROJECT_ID_KEY)
        self.pagesize = conf.get_int(
            BigQueryTableUsageExtractor.PAGE_SIZE_KEY,
            BigQueryTableUsageExtractor.DEFAULT_PAGE_SIZE)

        self.email_pattern = conf.get_string(BigQueryTableUsageExtractor.EMAIL_PATTERN, None)

        self.table_usage_counts = {}
        self._count_usage()
        self.iter = iter(self.table_usage_counts)

    def _count_usage(self):  # noqa: C901
        # type: () -> None
        count = 0
        for entry in self._retrieve_records():
            count += 1
            if count % self.pagesize == 0:
                LOGGER.info('Aggregated {} records'.format(count))

            try:
                job = entry['protoPayload']['serviceData']['jobCompletedEvent']['job']
            except Exception:
                # Skip the record if the record missing certain fields
                continue
            if job['jobStatus']['state'] != 'DONE':
                # This job seems not to have finished yet, so we ignore it.
                continue
            if len(job['jobStatus'].get('error', {})) > 0:
                # This job has errors, so we ignore it
                continue

            email = entry['protoPayload']['authenticationInfo']['principalEmail']
            refTables = job['jobStatistics'].get('referencedTables', None)

            if not refTables:
                # Query results can be cached and if the source tables remain untouched,
                # bigquery will return it from a 24 hour cache result instead. In that
                # case, referencedTables has been observed to be empty:
                # https://cloud.google.com/logging/docs/reference/audit/bigquery/rest/Shared.Types/AuditData#JobStatistics
                continue

            # if email filter is provided, only the email matched with filter will be recorded.
            if self.email_pattern:
                if not re.match(self.email_pattern, email):
                    # the usage account not match email pattern
                    continue

            numTablesProcessed = job['jobStatistics']['totalTablesProcessed']
            if len(refTables) != numTablesProcessed:
                LOGGER.warn('The number of tables listed in job {job_id} is not consistent'
                            .format(job_id=job['jobName']['jobId']))

            for refTable in refTables:
                key = TableColumnUsageTuple(database='bigquery',
                                            cluster=refTable['projectId'],
                                            schema=refTable['datasetId'],
                                            table=refTable['tableId'],
                                            column='*',
                                            email=email)

                new_count = self.table_usage_counts.get(key, 0) + 1
                self.table_usage_counts[key] = new_count

    def _retrieve_records(self):
        # type: () -> Optional[Dict]
        """
        Extracts bigquery log data by looking at the principalEmail in the
        authenticationInfo block and referencedTables in the jobStatistics.

        :return: Provides a record or None if no more to extract
        """
        body = {
            'resourceNames': [
                'projects/{projectid}'.format(projectid=self.projectid)
            ],
            'pageSize': self.pagesize,
            'filter': 'resource.type="bigquery_resource" AND '
                      'protoPayload.methodName="jobservice.jobcompleted" AND '
                      'timestamp >= "{timestamp}"'.format(timestamp=self.timestamp)
        }
        for page in self._page_over_results(body):
            for entry in page['entries']:
                yield(entry)

    def extract(self):
        # type: () -> Optional[tuple]
        try:
            key = next(self.iter)
            return key, self.table_usage_counts[key]
        except StopIteration:
            return None

    def _page_over_results(self, body):
        # type: (Dict) -> Optional[Dict]
        response = self.logging_service.entries().list(body=body).execute(
            num_retries=BigQueryTableUsageExtractor.NUM_RETRIES)
        while response:
            if 'entries' in response:
                yield response

            try:
                if 'nextPageToken' in response:
                    body['pageToken'] = response['nextPageToken']
                    response = self.logging_service.entries().list(body=body).execute(
                        num_retries=BigQueryTableUsageExtractor.NUM_RETRIES)
                else:
                    response = None
            except Exception:
                # Add a delay when BQ quota exceeds limitation
                sleep(BigQueryTableUsageExtractor.DELAY_TIME)

    def get_scope(self):
        # type: () -> str
        return 'extractor.bigquery_table_usage'
