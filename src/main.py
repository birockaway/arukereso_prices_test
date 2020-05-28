import csv
import re
import os
import sys
from contextlib import contextmanager
from io import StringIO
import logging

import paramiko
from logstash_formatter import LogstashFormatterV1
from keboola import docker


@contextmanager
def sftp_connection(server_address, port_number, username, password_con, rsa, passphrase_key):
    logging.info('Establishing sftp connection.')
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.RSAKey.from_private_key(StringIO(rsa), password=passphrase_key)
    sftp_con = None
    try:
        ssh.connect(server_address, port=port_number, username=username,
                    password=password_con, pkey=pkey)
        sftp_con = ssh.open_sftp()
        yield sftp_con
    except Exception as exc:
        logging.error(f'Failed to establish SFTP connection. Exception {exc}')
    finally:
        if sftp_con is not None:
            logging.info('Closing sftp.')
            sftp_con.close()
        logging.info('Closing ssh.')
        ssh.close()


class ArukeresoProcessor:
    def __init__(self, columns_list, results_writer):
        self.logger = logging.getLogger(__name__)
        self.datadir = os.getenv('KBC_DATADIR', '/data/')
        cfg = docker.Config(self.datadir)
        parameters = cfg.get_parameters()
        # log parameters (excluding sensitive designated by '#')
        self.logger.info({k: v for k, v in parameters.items() if "#" not in k})
        self.previous_timestamp_filename = parameters.get('previous_timestamp_filename')
        self.filename_pattern = parameters.get('filename_pattern')
        self.server = parameters.get('server')
        self.port = int(parameters.get('port'))
        self.user = parameters.get('username')
        self.password = parameters.get('#password')
        self.passphrase = parameters.get('#passphrase')
        self.rsa_key = parameters.get('#key')
        self.sftp_folder = '/upload/'
        self.files_to_process = []
        self.last_timestamp = 0
        self.previous_timestamp = 0
        self.columns_list = columns_list
        self.results_writer = results_writer
        (self.common_fields, self.highlighted_fields,
         self.cheapest_fields, self.mall_fields,
         self.constant_fields, self.observed_fields) = None, None, None, None, None, None

    def define_field_mappings(self):
        self.common_fields = {
            'ItemCode': 'MATERIAL',
            'EAN': 'EAN',
            'AKIdentifier': 'CSE_ID',
            'AKCategoryName': 'CATEGORY_NAME',
            'Rating': 'RATING',
            'ReviewCount': 'REVIEW_COUNT'
        }

        self.highlighted_fields = [{
            f'Highlighted{i} EshopName': 'ESHOP',
            f'Highlighted{i} Price': 'PRICE',
            f'Highlighted{i} Stock': 'AVAILABILITY',
            f'Highlighted{i} ShippingPrice': 'SHIPPING_PRICE'
        } for i in range(1, 4)]

        self.observed_fields = [{
            f'Observed{i} Name': 'ESHOP',
            f'Observed{i} Price': 'PRICE',
            f'Observed{i} Stock': 'AVAILABILITY',
            f'Observed{i} ShippingPrice': 'SHIPPING_PRICE'
        } for i in range(1, 6)]

        self.cheapest_fields = [{
            'Cheapest EshopName': 'ESHOP',
            'Cheapest Price': 'PRICE',
            'Cheapest Stock': 'AVAILABILITY',
            'Cheapest ShippingPrice': 'SHIPPING_PRICE'
        }]

        self.mall_fields = [{'Price': 'PRICE', 'Position': 'POSITION'}]

        self.constant_fields = {'COUNTRY': 'HU', 'DISTRCHAN': 'MA', 'SOURCE': 'arukereso', 'FREQ': 'd'}

    def get_previous_last_timestamp(self):
        with open(f'{self.datadir}in/tables/{self.previous_timestamp_filename}') as input_file:
            previous_timestamp_list = [
                str(ts.replace('"', ''))
                for ts
                # read all input file rows, except the header
                in input_file.read().split(os.linesep)[1:]
            ]
            self.previous_timestamp = float(previous_timestamp_list[0])

    def download_new_files(self):
        last_timestamp = self.previous_timestamp

        destroot = f'{self.datadir}in/tables/downloaded_csvs'
        if not os.path.exists(destroot):
            os.makedirs(destroot)

        # NB: original script downloaded both from upload and upload/archive
        # archive seems to contain only records that are several days old
        with sftp_connection(self.server, self.port, self.user, self.password, self.rsa_key, self.passphrase) as sftp:
            for file in sftp.listdir_attr(self.sftp_folder):
                modified_time = file.st_mtime
                if (modified_time > self.previous_timestamp) and file.filename.startswith(self.filename_pattern):
                    if modified_time > last_timestamp:
                        last_timestamp = modified_time
                    sourcepath = f'{self.sftp_folder}{file.filename}'
                    self.logger.info(f'Downloading file {sourcepath}')
                    destpath = f'{destroot}/{file.filename}'
                    self.files_to_process.append(destpath)
                    sftp.get(sourcepath, destpath)
        self.last_timestamp = last_timestamp

    def process_line(self, line, **kwargs):
        processed_eshops = []
        results = []
        # the order is important
        # if highlighted, we want to preserve the info adn ignore other records for the same shop
        for mapping in self.highlighted_fields + self.observed_fields + self.cheapest_fields + self.mall_fields:
            full_mapping = {**self.common_fields, **mapping}
            shop_data = {
                full_mapping[key]: line[key]
                for key in full_mapping.keys()
            }
            if mapping == self.mall_fields[0]:
                shop_data['ESHOP'] = 'mall.hu'
                shop_data['AVAILABILITY'] = ''
            if shop_data['ESHOP'] != '' and shop_data['ESHOP'] not in processed_eshops:
                if 'Highlighted' in list(mapping.keys())[0]:
                    shop_data['HIGHLIGHTED_POSITION'] = re.findall(
                        r'\d+',
                        list(mapping.keys())[0])[0]
                shop_data[
                    'STOCK'] = 1 if shop_data['AVAILABILITY'] == 'instock' else 0
                shop_data['TS'] = kwargs['file_timestamp']
                shop_data['SOURCE_ID'] = kwargs['filename']
                shop_result = {k: v
                               for k, v in {**self.constant_fields, **shop_data}.items()
                               if k in self.columns_list}
                processed_eshops.append(shop_result['ESHOP'])
                results.append(shop_result)
        return results

    def get_file_dicts(self, filepath):
        name = filepath.split('/')[-1]
        with open(filepath, 'r') as fl:
            # data for shops start at the second line
            timestamp = fl.readline().strip('\n')
            reader = csv.DictReader(fl, delimiter=';')
            for line in reader:
                line_dicts = self.process_line(line,
                                               file_timestamp=timestamp,
                                               filename=name)
                yield line_dicts

    def write_new_last_timestamp(self):
        self.logger.info('Processing done. Writing last timestamp.')
        with open(f'{self.datadir}out/tables/arukereso_last_timestamp.csv', 'w+') as fo:
            dict_writer = csv.DictWriter(fo, fieldnames=['max_timestamp_this_run'])
            dict_writer.writeheader()
            dict_writer.writerow({'max_timestamp_this_run': self.last_timestamp})

    def produce_results(self):
        self.define_field_mappings()
        self.get_previous_last_timestamp()
        self.download_new_files()
        if not self.files_to_process:
            self.logger.info('No new files to process. Exiting.')
            sys.exit(0)
        else:
            self.logger.info(f'Downloaded {len(self.files_to_process)} files.')
            self.results_writer.writeheader()
            for file in self.files_to_process:
                self.logger.info(f'Processing file: {file}')
                try:
                    for result_dicts in self.get_file_dicts(file):
                        self.results_writer.writerows(result_dicts)
                except Exception as e:
                    self.logger.error(f'Failed to process file: {file}. Exception {e}.')
            self.write_new_last_timestamp()


if __name__ == '__main__':
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    formatter = LogstashFormatterV1()

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level="DEBUG")
    colnames = ['AVAILABILITY',
                'COUNTRY',
                'CSE_ID',
                'CSE_URL',
                'DISTRCHAN',
                'ESHOP',
                'FREQ',
                'HIGHLIGHTED_POSITION',
                'MATERIAL',
                'POSITION',
                'PRICE',
                'RATING',
                'REVIEW_COUNT',
                'SOURCE',
                'SOURCE_ID',
                'STOCK',
                'TOP',
                'TS',
                'URL', ]
    with open(f'{os.getenv("KBC_DATADIR")}out/tables/results.csv', 'w') as outfile:
        writer = csv.DictWriter(outfile, fieldnames=colnames)
        arukereso_prices = ArukeresoProcessor(columns_list=colnames, results_writer=writer)
        arukereso_prices.produce_results()
