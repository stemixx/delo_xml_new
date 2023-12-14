import os
import logging
import sqlite3
import time
import json
import re
import pytz
import shutil
import configparser
import uuid
from lxml import etree
from datetime import datetime
from string import Template
from dateutil import parser as dateutil_parser
from email.mime.text import MIMEText
import schedule
import smtplib


def guid_separate(guid):
    return f"{guid[0:8]}-{guid[8:12]}-{guid[12:16]}-{guid[16:20]}-{guid[20:]}"


def str2int(s, default=0):
    """
    Переводит строку s в целое число. Если это не удается (s не содержит
    числа), возвращает значение default.

    Параметры:
    * s - строка, из которой нужно получить число;
    * default - результат по-умолчанию, целое число.

    Примеры использования:
    >>> str2int("45")
    45
    >>> str2int("45см")
    45
    >>> str2int("Рост: 45 см")
    45
    >>> str2int("Рост: -", 50)
    50
    >>> str2int("1+3")
    4
    """
    if '+' in s:
        try:
            sum_of_numbers = 0
            numbers = re.findall(r'\d+', s)
            for number in numbers:
                sum_of_numbers += int(number)
            return sum_of_numbers
        finally:
            pass
    return int((re.findall(r'\d+', s) or [default])[0])


class ConfParser:
    "Работа с файлом конфигурации"

    config_file = 'config.ini'
    config = configparser.ConfigParser()

    def __init__(self):
        self.read_inifile()

    def read_inifile(self):
        self.config = configparser.ConfigParser()
        self.config.read(self.config_file, encoding='utf-8')

    def create_section(self, section_name):
        self.config.add_section(section_name)

    def write_to_inifile(self, section_name, parameter, value):
        self.config.set(section_name, parameter, value)
        with open(self.config, 'w') as configfile:
            self.config.write(configfile)


class Logger:
    "Запись сообщений в лог-файл и вывод в терминал"

    def __init__(self):
        self.config_parser = ConfParser()
        if self.config_parser:
            self.dir = os.path.join(self.config_parser.config.get('Logger', 'log_dir'))
            self.file = os.path.join(self.config_parser.config.get('Logger', 'log_filename'))
            self.log_file = os.path.join(self.dir, self.file)
        else:
            self.log_file = 'log.txt'

        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s. %(message)s', datefmt='%d.%m.%Y %H:%M:%S')

        file_handler = logging.FileHandler(self.log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def write(self, message):
        self.logger.info(message)


logger = Logger()


class Mailer:
    def __init__(self, regnumber, mailtext):
        self._config_parser = ConfParser()
        if self._config_parser:
            self._server_host = self._config_parser.config.get('Mailer', 'server_host')
            self._server_address = self._config_parser.config.get('Mailer', 'server_address')
            self._RECIPIENTS = self._config_parser.config.items('Recipients')
            self._RECIPIENTS_CLERKS = self._get_recipients_clerks_dict(
                self._config_parser.config.items('RecipientsClerks')
            )
        self._regnumber = regnumber
        self._mailtext = mailtext

    # def _send_mail(self):
    #     try:
    #         self._connection = smtplib.SMTP(self._server_host)
    #     except Exception as e:
    #         print(e)
    #     if self._connection:
    #         for address in self._RECIPIENTS:
    #             self._msg = MIMEText(self._mailtext)
    #             self._msg['Subject'] = f'отказ в регистрации {self._regnumber}'
    #             self._msg['From'] = self._server_address
    #             # if self._config_parser:
    #             self._msg['To'] = address[1]
    #             self._connection.sendmail(self._server_address, [address[1]], self._msg.as_string())
    #             # else:
    #             #     self._msg['To'] = address
    #             #     self._connection.sendmail(self._server_address, [address], self._msg.as_string())
    #
    #         self._connection.quit()
    #     else:
    #         print("Error: Cannot connect to Mail server")

    def _get_recipients_clerks_dict(self, recipients):
        result = {}
        for recipient in recipients:
            result[recipient[0]] = json.loads(recipient[1])
        return result

    def _get_addresses(self, reg_number):
        '''
        Возвращает список адресов конкретного департамента согласно индексатору документов
        '''
        result = []
        for index, addresses in list(self._RECIPIENTS_CLERKS.items()):
            if reg_number.startswith(index):
                result = addresses
                break
        return result

    def _send_mail_to_clerks(self):
        # рассылка отказов в регистрации регистраторам исходящих документов согласно адресам, указанным в config.ini
        reg_number = self._regnumber
        # получаем список адресов для рассылки
        addresses = self._get_addresses(reg_number)
        if addresses:
            try:
                self._connection = smtplib.SMTP(self._server_host)
            except Exception as e:
                print(e)
            if self._connection:
                self._msg = MIMEText(self._mailtext)
                self._msg['Subject'] = f'отказ в регистрации {self._regnumber}'
                self._msg['From'] = self._server_address
                self._msg['To'] = ', '.join(addresses)
                self._connection.sendmail(self._server_address, addresses, self._msg.as_string())
                self._connection.quit()
            else:
                logger.write('Невозможно установить соединение с почтовым сервером для отправки сообщения об отказе '
                             'в регистрации')


class MessageBroker:
    ""

    def __init__(self):

        self._config_parser = ConfParser()
        self._is_debug_mode = self._config_parser.config.getboolean('MessageBroker', 'is_debug_mode')
        self._is_custom_medo_acknowledgment = self._config_parser.config.getboolean(
            'MessageBroker',
            'is_custom_medo_acknowledgment'
        )
        self._time_interval = self._config_parser.config.getint('Daemon', 'time_interval')
        self._agv_addresses = self._config_parser.config.get('MessageBroker', 'agv_addresses')
        self._sqlite_filename = self._config_parser.config.get('MessageBroker', 'sqlite_filename')

        if self._is_debug_mode:
            self._DIRS = {
                'SEND_DIR': self._config_parser.config.get('MessageBroker', 'debug_send_dir'),
                # Дело сообщения и Квитанции на отправку \send
                'RECEIVE_DIR': self._config_parser.config.get('MessageBroker', 'debug_receive_dir'),
                # МЭДО сообщения и квитанции для приема \receive
                'ARCHIVE_DIR': self._config_parser.config.get('MessageBroker', 'debug_archive_dir'),
                # Архив ДЕЛО сообщений, которые УСПЕШНО отправлены \archive
                'ARCHIVE_MEDO_DIR': self._config_parser.config.get('MessageBroker', 'debug_archive_medo_dir'),
                # Архив МЭДО сообщений, которые УСПЕШНО приняты \archive_medo
                'MEDO_SEND_DIR': self._config_parser.config.get('MessageBroker', 'debug_medo_send_dir'),
                # МЭДО сообщения, полученные из наших ДЕЛО сообщений. Их нужно отправить. \medo_send
                'DELO_RECEIVE_DIR': self._config_parser.config.get('MessageBroker', 'debug_delo_receive_dir'),
                # ДЕЛО сообщения, полученные из их МЭДО сообщений. Их нужно загрузить в ДЕЛО. \delo_receive
                'DELO_RECEIVE_DIR_REPORT': self._config_parser.config.get('MessageBroker',
                                                                          'debug_delo_receive_dir_report'),
                # delo_receive_dir_report
                'TEMPLATES_DIR': self._config_parser.config.get('MessageBroker', 'templates_dir'),
                'ERRORS_DELO': self._config_parser.config.get('MessageBroker', 'debug_errors_delo'),
                # папка ошибочных пакетов по формату Дело \errors\delo
                'ERRORS_MEDO': self._config_parser.config.get('MessageBroker', 'debug_errors_medo')
                # папка ошибочных пакетов по формату Мэдо \errors\medo
            }
        else:
            self._DIRS = {
                'SEND_DIR': self._config_parser.config.get('MessageBroker', 'send_dir'),
                # Дело сообщения и Квитанции на отправку D:\SEVDELO\Transport\Archive\Out
                'RECEIVE_DIR': self._config_parser.config.get('MessageBroker', 'receive_dir'),
                # МЭДО сообщения и квитанции для приема Z:\MEDO_OUT
                'ARCHIVE_DIR': self._config_parser.config.get('MessageBroker', 'archive_dir'),
                # Архив ДЕЛО сообщений, которые УСПЕШНО отправлены \archive
                'ARCHIVE_MEDO_DIR': self._config_parser.config.get('MessageBroker', 'archive_medo_dir'),
                # Архив МЭДО сообщений, которые УСПЕШНО? приняты \archive_medo
                'MEDO_SEND_DIR': self._config_parser.config.get('MessageBroker', 'medo_send_dir'),
                # МЭДО сообщения, полученные из наших ДЕЛО сообщений. Их нужно отправить. Z:\MEDO_IN
                'DELO_RECEIVE_DIR': self._config_parser.config.get('MessageBroker', 'delo_receive_dir'),
                # ДЕЛО сообщения, полученные из их МЭДО сообщений. Их нужно загрузить в ДЕЛО. D:\SEVDELO\DocumentIn
                'DELO_RECEIVE_DIR_REPORT': self._config_parser.config.get('MessageBroker',
                                                                          'delo_receive_dir_report'),
                # D:\SEVDELO\ReportIn
                'TEMPLATES_DIR': self._config_parser.config.get('MessageBroker', 'templates_dir'),
                'ERRORS_DELO': self._config_parser.config.get('MessageBroker', 'errors_delo'),
                # папка ошибочных пакетов по формату Дело errors\delo
                'ERRORS_MEDO': self._config_parser.config.get('MessageBroker', 'errors_medo')
                # папка ошибочных пакетов по формату Мэдо errors\medo
            }

    def get_address_uid(self, transport_uid):
        with open(self._agv_addresses, "r", encoding='utf-8') as address_file:
            data = json.load(address_file)
        try:
            if data[transport_uid]:
                return data[transport_uid]['delo_dep_id']
            else:
                return None
        except Exception:
            logger.write(f"Не найден id АГВ по transport uid {transport_uid}")
            return None

    def get_address_deloname(self, transport_uid):
        with open(self._agv_addresses, "r", encoding='utf-8') as address_file:
            data = json.load(address_file)
        try:
            if data[transport_uid]:
                return data[transport_uid]['delo_name']
            else:
                return None
        except Exception:
            logger.write(f"Не найдено наименование органа АГВ по transport uid {transport_uid}")
            return None

    def get_address_docgroup(self, transport_uid):
        with open(self._agv_addresses, "r", encoding='utf-8') as address_file:
            data = json.load(address_file)
        try:
            if data[transport_uid]:
                return data[transport_uid]['delo_doc_group']
            else:
                return None
        except Exception:
            logger.write(f"Не найдена группа документов АГВ по transport uid {transport_uid}")
            return None

    def get_agv_address_name_in_medo(self, delo_dep_id):
        # Получаем из json файла наименование органа АГВ, как он именуется в Правительстве
        with open(self._agv_addresses, "r", encoding='utf-8') as address_file:
            data = json.load(address_file)
        try:
            for delo_dep_name in data.keys():
                if data[delo_dep_name]["delo_dep_id"] == delo_dep_id:
                    return delo_dep_name
            return None
        except Exception:
            logger.write(
                f"Не найдено наименование органа АГВ (как он именуется в Правительстве) по delo_dep_id {delo_dep_id}"
            )
            return None

    def set_message_info(self, transport_guid, document_type, **kwargs):
        cursor = self._sqlite_connection.cursor()
        if document_type == "delo":
            cursor.execute('''insert into xfers values (NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)''', (
                transport_guid,
                kwargs['return_id'],
                kwargs['registration_number'],
                kwargs['registration_date'],
                kwargs['document_uid'],
                kwargs['sender_contact_pickle'],
                kwargs['recipient_contact_pickle'],
                kwargs['document_group']))
        else:
            cursor.execute('''insert into xfers values (NULL, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?)''', (
                transport_guid,
                kwargs['return_id'],
                kwargs['document_id'],
                kwargs['remote_reg_number']))

        self._sqlite_connection.commit()

    def get_message_info_by_transport_uid(self, transport_uid):
        cursor = self._sqlite_connection.cursor()

        cursor.execute('''select * from xfers where transport_uid=?''', (transport_uid,))
        return cursor.fetchone()

    def get_message_info_by_document_uid(self, document_uid):
        cursor = self._sqlite_connection.cursor()

        cursor.execute('''select * from xfers where document_uid=?''', (document_uid,))
        return cursor.fetchone()

    def get_typical_reason_of_refused(self, reason_of_refused):
        typical_reason_of_refused_list = [
            "Не подлежит регистрации",
            "Ошибка адресации",
            "Не указан корреспондент",
            "Нет искового заявления",
            "Отсутствует текст",
            "Нет подписи",
            "Приложение отсутствует",
            "Листаж приложения указан ошибочно",
            "Несовпадение реквизитов приложения с приложенными документами",
            "Нет № и даты",
            "Не полная комплектация",
            "Дата документа неразборчива",
            "Исходящий номер документа неразборчив",
            "Отсутствует (не видна) подпись",
            "Перевернуты страницы",
            "Продублированы страницы",
            "В приложении проект нормативного акта",
            "Документ ДСП",
            "Документ и/или приложение отсканированы с оборотом",
            "Документ неверно адресован",
            "Документ получен ранее на бумажном носителе",
            "Документ зарегистрирован ранее, текст взамен разосланного загружен",
            "Количество страниц в тексте документа не соответствует заявленному",
            "Несоответствие переданных реквизитов тексту документа",
            "Отсутствует приложение, указанное в документе",
            "Текст неразборчив",
            "Текст отсутствует",
            "Текст не соответствует регламенту"
        ]
        if reason_of_refused in typical_reason_of_refused_list:
            return reason_of_refused
        else:
            return 'Ошибка адресации'

    def _process_send_folder_from_delo(self):
        "Обработка директории отправляемых писем ДЕЛО"
        failure = ""
        logger.write('Обработка директории исходящих писем Дело...')
        send_dir_delo = self._DIRS['SEND_DIR']
        # Просмотрим все директории для отправки писем Дело
        for data_dir in os.listdir(send_dir_delo):
            for package_name_delo in os.listdir(os.path.join(send_dir_delo, data_dir)):
                applied_files_count = 0
                is_report_delo = False
                # Переименуем файлы вложений.
                if os.path.isdir(os.path.join(os.path.join(send_dir_delo, data_dir), package_name_delo)):
                    for insidefilename_delo in os.listdir(
                            os.path.join(os.path.join(send_dir_delo, data_dir), package_name_delo)
                    ):
                        applied_files_count = applied_files_count + 1
                        # Уберем служебные индексы [1],[2], ... прикрепленные системой Дело.
                        if "[" in insidefilename_delo and "]" in insidefilename_delo:
                            delo_filename_with_index = os.path.join(
                                os.path.join(os.path.join(send_dir_delo, data_dir), package_name_delo),
                                insidefilename_delo
                            )
                            delo_filename_without_index = re.sub('\[\d+\]', '', delo_filename_with_index)
                            if delo_filename_without_index:
                                try:
                                    os.rename(delo_filename_with_index, delo_filename_without_index)
                                except FileExistsError:
                                    delo_filename_without_index = re.sub('\[\d+\]', f'-{applied_files_count}', delo_filename_with_index)
                                    os.rename(delo_filename_with_index, delo_filename_without_index)
                                    logger.write(f"Внимание. Название файла {insidefilename_delo} повторяется. "
                                                 f"Переименовываем.")
                        # if insidefilename.endswith(".pdf.pdf") or insidefilename.endswith(".PDF.pdf"):
                        #     failure = f"One or more applied files have a bad extension (.pdf.pdf) {insidefilename}"
                        if insidefilename_delo == "Report.xml":
                            is_report_delo = True
                    if not is_report_delo and applied_files_count <= 1:
                        logger.write(f"Внимание. В директории {package_name_delo} нет прикреплённых файлов. Нечего "
                                     f"отправлять")
                        break

                    if failure != "":
                        logger.write(f"Исходящий пакет: {package_name_delo} Ошибка: {failure}.")
                        break
                    else:
                        # Отправим сообщения
                        logger.write(f"Отправка исходящего пакета {package_name_delo}...")
                        DeloMessage(
                            delo_message_uid=package_name_delo,
                            message_broker=self,
                            current_process_delo_data_dir=os.path.join(send_dir_delo, data_dir)
                        ).send()

    def _process_receive_folder_from_medo(self):
        "Обработка директории входящих писем из МЭДО"
        # Просмотрим все имена в директории входящих писем МЭДО
        logger.write('Обработка директории входящих писем МЭДО...')
        receive = self._DIRS['RECEIVE_DIR']
        for filename in os.listdir(receive):
            # Выберем директории (их названия и есть message_uid)
            if os.path.isdir(os.path.join(receive, filename)):
                # Пишем обход пустого пакета, если папка пустая - переходим к следующему пакету
                if os.listdir(os.path.join(receive, filename)):
                    # Примем сообщения
                    # for attached_filename in os.listdir(os.path.join(receive, filename)):
                    #     if ".doc.doc" in attached_filename or ".pdf.pdf" in attached_filename or ".tif.tif" in attached_filename or ".txt.txt" in attached_filename:
                    #         path = os.path.join(os.path.join(receive, filename), attached_filename)
                    #         path_new = path[:-4]
                    #         os.rename(path, path_new)
                    logger.write(f"Обработка входящего пакета: {filename}")
                    MedoMessage(medo_message_uid=filename, message_broker=self).receive()
                else:
                    continue

    def _check_receive_folder(self):
        try:
            if not os.path.exists(self._DIRS['RECEIVE_DIR']):
                logger.write("Ошибка. Нет доступа к директории получения писем.")
        except Exception as e:
            logger.write(e)
            return False
        return True

    def _check_send_folder(self):
        try:
            if not os.path.exists(self._DIRS['SEND_DIR']):
                logger.write("Ошибка. Нет доступа к директории отправления писем.")
        except Exception as e:
            logger.write(e)
            return False
        return True

    def run(self):
        self._sqlite_connection = sqlite3.connect(self._sqlite_filename)
        if self._sqlite_connection:
            if self._check_receive_folder():
                self._process_receive_folder_from_medo()
            if self._check_send_folder():
                self._process_send_folder_from_delo()
            self._sqlite_connection.close()
        else:
            logger.write(f"Ошибка. Нет подключения к базе данных {self._sqlite_filename}")


class XMLWorker:

    def __init__(self):
        self._namespaces_delo = {'sev': 'http://www.eos.ru/2010/sev'}
        self._namespaces_medo = {'xdms': "http://www.infpres.com/IEDMS"}

    def xml_change_by_template(self, xml_source, xslt_template):
        # Загрузим файл-источник Документа/Квитанции
        source_xml_tree = etree.parse(xml_source)

        # Строка ниже для проверки результата парсинга исходного xml в виде строки
        # source_xml_tree_str = etree.tostring(source_xml_tree, encoding='unicode')

        # Загрузим шаблон преобразования источника xml в xml назначения
        xslt_tree = etree.parse(xslt_template)
        # Создадим функцию преобразования
        transform = etree.XSLT(xslt_tree)
        # Преобразуем исходный xml по шаблону преобразования
        converted_xml_tree = transform(source_xml_tree)

        # Строка ниже для проверки результата преобразованного xml
        # result_tree_str = etree.tostring(result_tree, encoding='unicode')

        return converted_xml_tree

    def get_xml_filename_with_path(self, path_to_package):
        for filename in os.listdir(path_to_package):
            if re.search(r"\.xml$", filename):
                xml_filename = os.path.join(path_to_package, filename)
                break
        else:
            xml_filename = None
            logger.write(f"Директория {path_to_package} не содержит xml-файла")

        return xml_filename

    def get_xml_message_type(self, system_name, xml_filename):
        xml_tree = etree.parse(xml_filename)
        if system_name == 'delo':
            if 'DocInfo.xml' in xml_filename:
                message_type = 'Документ'
            else:
                message_type = 'Уведомление'
        elif system_name == 'medo':
            message_type = xml_tree.xpath(
                '/xdms:communication/xdms:header/@xdms:type', namespaces=self._namespaces_medo)[0]
        else:
            message_type = None

        return message_type


class DeloMessage:
    "Создание МЭДО сообщения из ДЕЛО"

    def __init__(self, delo_message_uid, message_broker, current_process_delo_data_dir):
        self._delo_message_uid = delo_message_uid  # название пакета Дело и есть delo_message_uid
        self._message_broker = message_broker
        self._DIRS = message_broker._DIRS
        self._xml_worker = XMLWorker()
        self._current_process_delo_data_dir = current_process_delo_data_dir
        self._is_failure_by_delo = False
        self._is_ignore_by_delo = False
        self._already_exist_delo_message_in_acrhive = False
        self._xsd_filename_for_make_medo = os.path.join(self._DIRS['TEMPLATES_DIR'], 'IEDMS.xsd')
        self._namespaces_delo = {'sev': 'http://www.eos.ru/2010/sev'}
        self._namespaces_medo = {'xdms': "http://www.infpres.com/IEDMS"}
        self._delo_xml_filename = self._xml_worker.get_xml_filename_with_path(
            os.path.join(self._current_process_delo_data_dir, delo_message_uid)
        )
        self._delo_tree_xml = etree.parse(self._delo_xml_filename)
        self._message_type = self._xml_worker.get_xml_message_type(system_name='delo',
                                                                   xml_filename=self._delo_xml_filename)
        # Проверим с чем имеем дело - сообщение/квитанция/уведомление
        if self._message_type == 'Документ':
            # Сообщение Дело. Определяем xslt шаблон для него
            self._is_document_delo = True
            self._delo_to_medo_xslt_template_filename = os.path.join(self._DIRS['TEMPLATES_DIR'],
                                                                     'delo_docinfo_to_medo_document.xslt')

        elif self._message_type == 'Уведомление':
            # Квитанция или уведомление Дело
            self._is_document_delo = False

            # Определяем точный тип - квитанция или уведомление Дело. Если тег не найден - формируется пустой список
            reception_by_delo = self._delo_tree_xml.findall("*/sev:Reception", namespaces=self._namespaces_delo)
            # Определяем - принят документ МЭДО или игнорирован.
            document_accepted_by_delo = self._delo_tree_xml.findall("*/sev:Registration",
                                                                    namespaces=self._namespaces_delo)
            document_refused_by_delo = self._delo_tree_xml.findall("*/sev:Failure", namespaces=self._namespaces_delo)

            if reception_by_delo:
                # Квитанция Дело. Ставим флаги, что не уведомление Дело. Определяем xslt шаблон
                self._is_acknowledgment_delo = True
                self._is_notification_delo = False
                self._is_notification_by_delo_confirm = False
                self._is_notification_by_delo_refuse = False
                self._delo_to_medo_xslt_template_filename = os.path.join(
                    self._DIRS['TEMPLATES_DIR'], 'delo_report_to_medo_acknowledgment.xslt'
                )
            else:
                # Уведомление Дело. Ставим флаг, что не квитанция.
                self._is_notification_delo = True
                self._is_acknowledgment_delo = False
                if document_accepted_by_delo:
                    # Ставим флаг, что Дело уведомление о принятом МЭДО сообщении. Определяем xslt шаблон
                    self._is_notification_by_delo_confirm = True
                    self._is_notification_by_delo_refuse = False
                    self._delo_to_medo_xslt_template_filename = os.path.join(
                        self._DIRS['TEMPLATES_DIR'], 'delo_report_to_medo_notification_confirm.xslt'
                    )
                elif document_refused_by_delo:
                    # Ставим флаг, что Дело уведомление об отказе принятия МЭДО сообщения. Определяем xslt шаблон
                    self._is_notification_by_delo_refuse = True
                    self._is_notification_by_delo_confirm = False
                    self._delo_to_medo_xslt_template_filename = os.path.join(
                        self._DIRS['TEMPLATES_DIR'], 'delo_report_to_medo_notification_refuse.xslt'
                    )
                # Находим в уведомлении return_id, сформированный в ответ на документ МЭДО
                return_id_delo = self._delo_tree_xml.xpath(
                    '/sev:Report/sev:Header/@ReturnID', namespaces=self._namespaces_delo)[0]
                # По return_id уведомления Дело находим в базе информацию по входящему документу МЭДО
                incoming_medo_document_info = self._message_broker.get_message_info_by_transport_uid(return_id_delo)

                try:
                    # Если список incoming_medo_document_info сформировался, то должен быть document_uid МЭДО.
                    # Иначе - None
                    saved_medo_document_uid = incoming_medo_document_info[5]
                    # Если не находим document_uid МЭДО (список пустой), то ставим флаг игнорирования Делом
                    if not re.search(
                            r'[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}',
                            saved_medo_document_uid
                    ):
                        self._is_ignore_by_delo = True
                except Exception:
                    logger.write(
                        f"Для уведомления Дело {self._delo_message_uid} в базе не найдена информация о документе МЭДО по "
                        f"идентификатору ReturnID {return_id_delo}")
                    self._is_ignore_by_delo = True

        else:
            logger.write(f"Неизвестный формат Дело сообщения {delo_message_uid}")

    def _move_delo_message_to_archive(self):
        "Перемещение ДЕЛО сообщения в архивную папку"
        datestamp = datetime.now().strftime('%Y_%m_%d')
        try:
            # Если не создана поддиректория-текущая дата в архиве Дело, то создадим её
            if not os.path.exists(os.path.join(self._DIRS['ARCHIVE_DIR'], datestamp)):
                os.makedirs(os.path.join(self._DIRS['ARCHIVE_DIR'], datestamp))
            # Создадим в архиве Дело сообщений каталог по названию отправленного пакета в формате МЭДО
            medo_package_name_dir = os.path.join(self._DIRS['ARCHIVE_DIR'], datestamp, self._package_name_delo)
            os.makedirs(medo_package_name_dir)
            # Переместим в архив Дело.env файл Дело
            shutil.move(os.path.join(self._current_process_delo_data_dir, f"{self._delo_message_uid}.env"),
                        medo_package_name_dir)
            # Переместим в архив Дело каталог с отправленным Дело сообщением
            shutil.move(os.path.join(self._current_process_delo_data_dir, self._delo_message_uid),
                        medo_package_name_dir)

        except Exception as e:
            logger.write(
                f"Не удалось переместить пакет Дело "
                f"{os.path.join(self._DIRS['ARCHIVE_DIR'], datestamp, self._package_name_delo)} в архив")
            logger.write(e)
            self._already_exist_delo_message_in_acrhive = True

        # Если в директориях типа "2023.06.06 09-53" все файлы обработаны, то удаляем эти директории
        if not os.listdir(self._current_process_delo_data_dir):
            os.rmdir(self._current_process_delo_data_dir)

    def _move_delo_message_to_error(self):
        "Перемещение игнорируемого уведомления ДЕЛО сообщения в архивную папку."
        datestamp = datetime.now().strftime('%Y_%m_%d')
        try:
            # Если не создана поддиректория-текущая дата в errors/delo, то создадим её
            if not os.path.exists(os.path.join(self._DIRS['ERRORS_DELO'], datestamp)):
                os.makedirs(os.path.join(self._DIRS['ERRORS_DELO'], datestamp))
            # Переместим файл .env Дело в поддиректорию - текущая дата
            shutil.move(os.path.join(self._current_process_delo_data_dir, f"{self._delo_message_uid}.env"),
                        os.path.join(self._DIRS['ERRORS_DELO'], datestamp))
            # Переместим каталог с сообщением Дело
            shutil.move(os.path.join(self._current_process_delo_data_dir, self._delo_message_uid),
                        os.path.join(self._DIRS['ERRORS_DELO'], datestamp))

        except Exception as e:
            # Если деректорию создать не удалось, то выведем ошибку (скорее всего директория уже существует)
            logger.write(e)
            self._already_exist_delo_message_in_acrhive = True

        # Если в директориях типа "2023.06.06 09-53" все файлы обработаны, то перемещаем сами эти директории
        if not os.listdir(self._current_process_delo_data_dir):
            shutil.move(self._current_process_delo_data_dir, os.path.join(self._DIRS['ARCHIVE_DIR'], datestamp))

    def _create_medo_envelope_ini(self):
        "Создание файла envelope.ini в директории МЭДО для входящих"
        if self._is_document_delo:
            # Если дело документ, то сформируем envelope.ini МЭДО с информацией о приложенных файлах
            # Получим тему сообщения из сформированного МЭДО сообщения
            annotation = self._medo_message_tree.findall("*/xdms:annotation", namespaces=self._namespaces_medo)[0].text
            # Получим список приложенных файлов из МЭДО сообщения
            applied_files = self._medo_message_tree.findall("*/xdms:file", namespaces=self._namespaces_medo)
            applied_files_list = []
            for applied_file in applied_files:
                # Получим номер файла для записи в envelope.ini В МЭДО сообщении нумерация с 0
                # В envelope.ini нулевой файл - passport.xml, все приложения нумеруются с 1
                # поэтому увеличиваем номер файла из МЭДО сообщения на 1
                file_id = int(applied_file.attrib['{%s}localId' % self._namespaces_medo['xdms']]) + 1
                # Получим имя приложенного файла
                filename = applied_file.attrib['{%s}localName' % self._namespaces_medo['xdms']]
                # Добавим номер файла и имя файла в список
                applied_files_list.append([file_id, filename])

            # Отсортируем список приложенных файлов по их номерам
            applied_files_list.sort()
            # Соберем строку с перечислением приложенных файлов
            files_str = "0=passport.xml\n"
            for file_id, filename in applied_files_list:
                files_str += '%s=%s\n' % (file_id, filename)

        else:
            files_str = '0=passport.xml'
            # Квитанция
            if self._is_acknowledgment_delo:
                annotation = 'Квитанция'
            # Уведомление
            if self._is_notification_delo:
                annotation = 'Уведомление'
        # Подготовим атрибуты для подстановки
        attrs = {'TITLE': annotation,
                 'DATETIME': datetime.now().strftime('%d.%m.%Y %H:%M:%S'),
                 'FILES': files_str}

        # Сформируем содержимое файла envelope.ini на основании шаблона
        with open(os.path.join(self._DIRS['TEMPLATES_DIR'], 'envelope.ini'), 'r', encoding='utf-8') as f:
            envelope_content = Template(f.read()).substitute(attrs)

        # Запишем файл envelope.ini
        with open(
                os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name_delo, 'envelope.ini'),
                'w', encoding='utf-8'
        ) as f:
            f.write(envelope_content)

    def _copy_applied_delo_files_to_medo_dir(self):
        "Копирование приложенных файлов ДЕЛО сообщения в директорию МЭДО сообщения"
        for filename in os.listdir(os.path.join(self._current_process_delo_data_dir, self._delo_message_uid)):
            # Не нужно копировать файл DocInfo.xml Report.xml и файлы электронных подписей .sig and not filename.endswith('.sig')
            if filename != 'DocInfo.xml' and filename != 'Report.xml' and not filename.endswith('.sig'):
                shutil.copy(
                    os.path.join(self._current_process_delo_data_dir, self._delo_message_uid, filename),
                    os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name_delo, filename)
                )

    def _create_remote_medo_folder_from_delo_message(self):
        "Создание директории для МЭДО сообщения из исходного сообщения Дело"
        if not os.path.exists(os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name_delo)):
            os.makedirs(os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name_delo))

    def _save_final_medo_message_in_medo_folder(self):
        "Запись МЭДО сообщения в файл в директории отправки МЭДО"
        if self._is_document_delo or self._is_acknowledgment_delo or self._is_notification_delo:
            full_xml_filename = os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name_delo, 'passport.xml')

            # Запишем МЭДО сообщение в файл
            with open(full_xml_filename, 'wb') as f:
                self._medo_message_tree.write(f, pretty_print=True, encoding='windows-1251')

    def _create_medo_message_from_delo(self):
        "Создание МЭДО сообщения из ДЕЛО сообщения/квитанции"
        medo_from_delo_result_tree_xml = self._xml_worker.xml_change_by_template(
            xml_source=self._delo_xml_filename,
            xslt_template=self._delo_to_medo_xslt_template_filename
        )

        if self._is_document_delo:
            # Документ Дело
            delo_document_uid = self._delo_tree_xml.xpath('.//sev:Document/@UID', namespaces=self._namespaces_delo)[0]
            delo_department_uid = self._delo_tree_xml.xpath(
                '..//sev:DocInfo/sev:Header/sev:Sender/sev:Contact/sev:Organization/@UID',
                namespaces=self._namespaces_delo)[0]
            delo_organization_name_in_other_SED = (
                    self._message_broker.get_agv_address_name_in_medo(delo_department_uid) or ""
            )

            # Определим значение дополнительных атрибутов, которые будут подставлены в МЭДО сообщение
            attrs = {'ISO_DATETIME': datetime.now().replace(microsecond=0).isoformat(),
                     # Время формирования сообщения
                     'TRANSPORT_GUID': str(uuid.uuid4()).upper(),  # Транспортный GUID, он в примере UPPER CASE
                     'MESSAGE_GUID': guid_separate(delo_document_uid),  # str(uuid.uuid4()), # GUID документа
                     'AGV_GUID': delo_department_uid,  # GUID Администрации города Вологды. Постоянный.
                     'ORGANIZATION_NAME': delo_organization_name_in_other_SED
                     # наименование структурного подразделения АГВ (из json)
                     }
        else:
            # Квитанция/уведомление
            # Добавим недостающую дополнительную информацию
            delo_return_id = self._delo_tree_xml.xpath(
                '/sev:Report/sev:Header/@ReturnID',
                namespaces=self._namespaces_delo)[0]
            delo_department_uid = self._delo_tree_xml.xpath(
                '..//sev:Report/sev:Header/sev:Sender/sev:Contact/sev:Organization/@UID',
                namespaces=self._namespaces_delo)[0]

            outcoming_delo_message_info = self._message_broker.get_message_info_by_transport_uid(delo_return_id)
            delo_organization_name_in_other_SED = (
                    self._message_broker.get_agv_address_name_in_medo(delo_department_uid) or "")

            if outcoming_delo_message_info:
                # Уведомление
                if self._is_acknowledgment_delo:
                    date_time = medo_from_delo_result_tree_xml.xpath('.//xdms:acknowledgment/xdms:time',
                                                                     namespaces=self._namespaces_medo)[0]
                elif self._is_notification_by_delo_confirm:
                    date_time = \
                        medo_from_delo_result_tree_xml.xpath('.//xdms:notification/xdms:documentAccepted/xdms:time',
                                                             namespaces=self._namespaces_medo)[0]
                else:
                    date_time = \
                        medo_from_delo_result_tree_xml.xpath('.//xdms:notification/xdms:documentRefused/xdms:time',
                                                             namespaces=self._namespaces_medo)[0]
                date_time.text = date_time.text[:19]
                # Определим значение дополнительных атрибутов, которые будут подставлены в МЭДО сообщение
                if self._is_acknowledgment_delo:
                    return_id = outcoming_delo_message_info[2]  # GUID сообщения/пакета
                    attrs = {
                        'MESSAGE_UID': str(uuid.uuid4()).upper(),  # Транспортный GUID, он в примере UPPER CASE
                        'DOCUMENT_UID': return_id,  # GUID сообщения/пакета
                        'MSG_TYPE': 'Квитанция',
                        'AGV_GUID': delo_department_uid,  # GUID Администрации города Вологды. Постоянный.
                        'ORGANIZATION_NAME': delo_organization_name_in_other_SED
                        # наименование структурного подразделения АГВ (из json)
                    }
                elif self._is_notification_delo:
                    document_uid = outcoming_delo_message_info[5]  # GUID документа
                    if self._is_notification_by_delo_confirm:
                        attrs = {
                            'MESSAGE_UID': str(uuid.uuid4()).upper(),
                            # Транспортный GUID, он в примере UPPER CASE
                            'DOCUMENT_UID': document_uid,  # GUID документа
                            'AGV_GUID': delo_department_uid,  # GUID Администрации города Вологды. Постоянный.
                            'ORGANIZATION_NAME': delo_organization_name_in_other_SED
                            # наименование структурного подразделения АГВ (из json)
                        }
                    else:
                        refused_reason = self._message_broker.get_typical_reason_of_refused(
                            self._delo_tree_xml.xpath('//sev:Report/sev:Notification/sev:Failure',
                                                      namespaces=self._namespaces_delo)[0].text.strip())
                        attrs = {'MESSAGE_UID': str(uuid.uuid4()).upper(),
                                 # Транспортный GUID, он в примере UPPER CASE
                                 'DOCUMENT_UID': document_uid,  # GUID документа
                                 'AGV_GUID': delo_department_uid,  # GUID Администрации города Вологды. Постоянный.
                                 'REFUSED_REASON': refused_reason,  # причина отказа в регистрации
                                 'ORGANIZATION_NAME': delo_organization_name_in_other_SED
                                 # наименование структурного подразделения АГВ (из json)
                                 }
            else:
                self._is_failure_by_delo = True
                logger.write(
                    f"В базе данных не найдено информации для квитанции с transport_guid {delo_return_id}."
                )

        if not self._is_failure_by_delo:
            xml_template = Template(str(medo_from_delo_result_tree_xml))
            medo_from_delo_result_tree_xml = xml_template.substitute(attrs)
            medo_from_delo_result_tree_xml = etree.fromstring(medo_from_delo_result_tree_xml)
            medo_from_delo_result_tree_xml = medo_from_delo_result_tree_xml.getroottree()

        if self._is_document_delo:
            # Найдем атрибут "ЧИСЛО_СТРАНИЦ"
            pages = medo_from_delo_result_tree_xml.find(".//xdms:pages", namespaces=self._namespaces_medo)
            # Запишем в элемент "ЧИСЛО_СТРАНИЦ" значение типа str, преобразованного из pages
            # Функция str2int возвращает int --> число страниц или 0, поэтому преобразуем в str и сохраняем
            try:
                pages.text = str(str2int(pages.text))
            except Exception as e:
                logger.write(e)
                if pages.text == None:
                    logger.write('Состав страниц не указан. Ставим 1')
                    pages.text = '1'

        if not self._is_failure_by_delo:
            # Загрузим XSD схему для проверки МЭДО сообщения
            xmlschema_doc = etree.parse(self._xsd_filename_for_make_medo)
            xmlschema = etree.XMLSchema(xmlschema_doc)

            # Проверим МЭДО сообщение по XSD схеме
            if xmlschema.validate(medo_from_delo_result_tree_xml):
                # Проверка прошла успешно, можно сохранять сообщение
                if self._is_document_delo:
                    self._transport_guid = attrs['TRANSPORT_GUID']
                else:
                    self._transport_guid = attrs['MESSAGE_UID']
                self._medo_message_tree = medo_from_delo_result_tree_xml

                # Сохраним информацию об исходном письме для последующего формирования квитанции для ДЕЛО
                if self._is_document_delo:
                    return_id = str(self._delo_tree_xml.xpath('.//sev:Header/@ReturnID',
                                                              namespaces=self._namespaces_delo)[0])

                    document_uid = str(medo_from_delo_result_tree_xml.xpath('.//xdms:document/@xdms:uid',
                                                                            namespaces=self._namespaces_medo)[0])

                    document_group = self._delo_tree_xml.xpath('.//sev:DocumentList/sev:Document/sev:Group',
                                                               namespaces=self._namespaces_delo)[0].text

                    registration_number = \
                        self._delo_tree_xml.xpath('.//sev:DocumentList/sev:Document/sev:RegistrationInfo/sev:Number',
                                                  namespaces=self._namespaces_delo)[0].text

                    registration_date = \
                        self._delo_tree_xml.xpath('.//sev:DocumentList/sev:Document/sev:RegistrationInfo/sev:Date',
                                                  namespaces=self._namespaces_delo)[0].text

                    sender_contact = self._delo_tree_xml.xpath('*/sev:Sender/sev:Contact',
                                                               namespaces=self._namespaces_delo)[0]
                    recipient_contact = self._delo_tree_xml.xpath('*/sev:Recipient/sev:Contact',
                                                                  namespaces=self._namespaces_delo)[0]

                    sender_contact_pickle = etree.tostring(sender_contact, encoding='utf-8')
                    recipient_contact_pickle = etree.tostring(recipient_contact, encoding='utf-8')

                    kwargs = {
                        'return_id': return_id,
                        'document_uid': document_uid,
                        'sender_contact_pickle': sender_contact_pickle,
                        'recipient_contact_pickle': recipient_contact_pickle,
                        'registration_number': registration_number,
                        'registration_date': registration_date,
                        'document_group': document_group
                    }

                    self._message_broker.set_message_info(self._transport_guid, "delo", **kwargs)
                return 'Success'
            else:
                # Проверка не прошла, выведем причину непрохождения
                try:
                    xmlschema.assertValid(medo_from_delo_result_tree_xml)
                except Exception as e:
                    logger.write(f"XSD Error: {e}")

                return None
        else:
            return None

    def send(self):
        if not self._is_ignore_by_delo:
            result = self._create_medo_message_from_delo()
            if result:
                self._package_name_delo = self._transport_guid
                if self._is_document_delo:
                    self._package_name_delo = 'Document_' + self._package_name_delo
                elif self._is_acknowledgment_delo:
                    self._package_name_delo = 'Acknowledgment_' + self._package_name_delo
                elif self._is_notification_delo:
                    self._package_name_delo = 'Notification_' + self._package_name_delo
                # Создадим директории для передачи сообщения
                self._create_remote_medo_folder_from_delo_message()
                # Скопируем в директорию МЭДО сообщения приложенные файлы ДЕЛО сообщения
                self._copy_applied_delo_files_to_medo_dir()
                # Сохраним сообщение в директорию отправки МЭДО
                self._save_final_medo_message_in_medo_folder()
                # Создадим в директории МЭДО сообщения файл envelope.ini
                self._create_medo_envelope_ini()
                # Переместим сообщение ДЕЛО в архивнут папку, чтобы не отправлять его еще раз
                self._move_delo_message_to_archive()
                if not self._already_exist_delo_message_in_acrhive:
                    logger.write(f"Пакет {self._package_name_delo} успешно отправлен")

        else:
            datestamp = datetime.now().strftime('%Y_%m_%d')
            self._move_delo_message_to_error()
            logger.write(f"Внимание: пакет {self._delo_message_uid} игнорирован. ")
            if not self._already_exist_delo_message_in_acrhive:
                logger.write(
                    f"Внимание: пакет {self._delo_message_uid} перемещён в директорию"
                    f" {os.path.join(self._DIRS['ERRORS_DELO'], datestamp)}"
                )
            else:
                logger.write(
                    f"Проверьте директорию {os.path.join(self._DIRS['ERRORS_DELO'], datestamp)}. Возможно пакет "
                    f"{self._delo_message_uid} уже присутствует там"
                )


class MedoMessage:
    "Создание Дело сообщения из МЭДО"

    def __init__(self, medo_message_uid, message_broker):
        self._namespaces_delo = {'sev': 'http://www.eos.ru/2010/sev'}
        self._namespaces_medo = {'xdms': "http://www.infpres.com/IEDMS"}
        self._datestamp = datetime.now().strftime('%Y_%m_%d')
        self._medo_message_uid = medo_message_uid
        self._message_broker = message_broker
        self._DIRS = message_broker._DIRS
        self._xml_work = XMLWorker()
        self._is_failure = False
        self._already_exist = False
        self._medo_xml_filename_with_path = self._xml_work.get_xml_filename_with_path(
            path_to_package=os.path.join(self._DIRS['RECEIVE_DIR'], medo_message_uid)
        )
        self._medo_message_type = self._xml_work.get_xml_message_type(
            xml_filename=self._medo_xml_filename_with_path,
            system_name='medo'
        )
        self._medo_xml_tree = etree.parse(self._medo_xml_filename_with_path)
        # Если активирована опция для формирования уведомлений, то установим соответствующий шаблон
        if self._message_broker._is_custom_medo_acknowledgment:
            self._xslt_template_filename_for_custom_acknowledgment = os.path.join(
                self._DIRS['TEMPLATES_DIR'], 'medo_document_to_medo_acknowledgment.xslt'
            )
            self._xsd_acknowledgment_filename = os.path.join(self._DIRS['TEMPLATES_DIR'], 'IEDMS.xsd')

        if self._medo_message_type == 'Документ':
            # Документ МЭДО
            self._is_medo_document = True
            self._medo_to_delo_xslt_template_filename = os.path.join(
                self._DIRS['TEMPLATES_DIR'], 'medo_document_to_delo_docinfo.xslt'
            )
            self._xsd_filename = os.path.join(self._DIRS['TEMPLATES_DIR'], 'DocumentInfo.xsd')
            self._is_ignore = False

        elif self._medo_message_type == 'Квитанция':
            # Квитанция о получении
            self._is_medo_document = False
            self._is_notification_medo = False
            self._is_acknowledgment = True
            self._medo_to_delo_xslt_template_filename = os.path.join(
                self._DIRS['TEMPLATES_DIR'], 'medo_acknowledgment_to_delo_report.xslt'
            )
            self._xsd_filename = os.path.join(self._DIRS['TEMPLATES_DIR'], 'ReportInfo.xsd')
            self._is_ignore = False

        elif self._medo_message_type == 'Уведомление':
            # Уведомление о регистрации или отказе от регистрации
            self._is_medo_document = False
            self._is_notification_medo = True
            self._is_acknowledgment = False

            document_accepted = self._medo_xml_tree.findall("*/xdms:documentAccepted", namespaces=self._namespaces_medo)
            document_refused = self._medo_xml_tree.findall("*/xdms:documentRefused", namespaces=self._namespaces_medo)
            # Если это уведомление об успешной регистрации, выберем шаблоны
            if document_accepted:
                self._is_notification_confirm = True
                self._is_notification_refuse = False
                self._medo_to_delo_xslt_template_filename = os.path.join(
                    self._DIRS['TEMPLATES_DIR'], 'medo_notification_to_delo_report_confirm.xslt'
                )
                self._xsd_filename = os.path.join(self._DIRS['TEMPLATES_DIR'], 'ReportInfo.xsd')
                self._is_ignore = False
            # Если это уведомление об отказе в регистрации, выберем шаблоны
            elif document_refused:
                self._is_notification_confirm = False
                self._is_notification_refuse = True
                self._medo_to_delo_xslt_template_filename = os.path.join(
                    self._DIRS['TEMPLATES_DIR'], 'medo_notification_to_delo_report_refuse.xslt'
                )
                self._xsd_filename = os.path.join(self._DIRS['TEMPLATES_DIR'], 'ReportInfo.xsd')
                self._is_ignore = False
            # Если уведомление о назначении исполнителя в Правительстве или еще какие-либо действия, то игнорируем,
            # перенесем в архив приема, но не принимаем в ДЕЛО
            else:
                self._is_ignore = True
                logger.write('Иное уведомление МЭДО.')
        # Тип документа определить не удалось
        else:
            self._is_ignore = True
            logger.write('Не удалось распознать тип документа.')

    # def _medo_source_xml_filename(self):
    #     # Ищем xml файл внутри директории входящего пакета, парсим
    #     self._medo_message_dir = os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid)
    #     for filename_medo in os.listdir(self._medo_message_dir):
    #         if re.search(r"\.xml$", filename_medo):
    #             medo_xml_filename = os.path.join(self._medo_message_dir, filename_medo)
    #             break
    #     else:
    #         logger.write(
    #             f"Директория входящих писем {os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid)} "
    #             f"не содержит xml-файла")
    #         medo_xml_filename = None
    #     return medo_xml_filename

    def _identify_medo_message_type(self):
        if self._medo_xml_filename_with_path:
            try:
                medo_message_type = self._medo_xml_tree.xpath(
                    '/xdms:communication/xdms:header/@xdms:type', namespaces=self._namespaces_medo)[0]
            except Exception as e:
                logger.write(f"Внимание: пакет {self._medo_message_uid} не распознан.\n{e}")
                self._move_medo_message_to_error()
                medo_message_type = None
        else:
            medo_message_type = None
        return medo_message_type

    def _move_medo_message_to_archive(self):
        "Перемещение МЭДО сообщения в архивную папку"
        # Переместим каталог с сообщением
        try:
            if not os.path.exists(os.path.join(self._DIRS['ARCHIVE_MEDO_DIR'], self._datestamp)):
                os.makedirs(os.path.join(self._DIRS['ARCHIVE_MEDO_DIR'], self._datestamp))
            shutil.move(os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid),
                        os.path.join(self._DIRS['ARCHIVE_MEDO_DIR'], self._datestamp))

        except Exception as e:
            logger.write(e)
            logger.write(
                f"Ошибка: проверьте директорию {os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid)}. "
                f"Возможно она уже существует"
            )
            self._already_exist = True

    def _move_medo_message_to_error(self):
        "Перемещение игнорируемого уведомления МЭДО в папку с ошибками МЭДО"
        # Переместим каталог с сообщением
        try:
            if not os.path.exists(os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp)):
                os.makedirs(os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp))
            shutil.move(
                os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid),
                os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp)
            )
            logger.write(
                f"Внимание: пакет {self._medo_message_uid} перемещён в директорию"
                f" {os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp, self._medo_message_uid)}"
            )
        except Exception as e:
            logger.write(e)
            logger.write(
                f"Проверьте директорию "
                f"{os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp)}. "
                f"Возможно пакет {self._medo_message_uid} уже присутствует там"
            )

    def _create_delo_env_file(self):
        """Создание файла .env"""
        if self._is_medo_document:
            # Получим тему сообщения из ДЕЛО сообщения
            attrs = {
                'SUBJECT':
                    self._delo_message_tree.findall(
                        "*/sev:Document/sev:RegistrationInfo/sev:Number", self._namespaces_delo
                    )[0].text,
                'TRANSPORT_GUID': self._transport_guid,
                'DATETIME': datetime.now(pytz.timezone('Etc/GMT-3')).isoformat()
            }
        else:
            # Квитанция
            attrs = {'TRANSPORT_GUID': self._transport_guid,
                     'DATETIME': datetime.now(pytz.timezone('Etc/GMT-3')).isoformat()
                     }
            # annotation = 'Квитанция'

        # Подготовим атрибуты для подстановки

        # Сформируем содержимое файла envelope.ini на основании шаблона
        with open(os.path.join(self._DIRS['TEMPLATES_DIR'], 'delo.env'), 'r') as f:
            envelope_content = Template(f.read()).substitute(attrs)

        # Запишем файл envelope.ini
        if self._is_medo_document:
            with open(os.path.join(self._DIRS['DELO_RECEIVE_DIR'], f"{self._transport_guid}.env"), 'w') as f:
                f.write(envelope_content)
        else:
            with open(os.path.join(self._DIRS['DELO_RECEIVE_DIR_REPORT'], f"{self._transport_guid}.env"), 'w') as f:
                f.write(envelope_content)

    def _copy_applied_files(self):
        "Копирование приложенных файлов МЭДО сообщения в директорию ДЕЛО сообщения"
        for filename in os.listdir(os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid)):
            # Не нужно копировать файл passport.xml envelope.ini
            if not filename.endswith('.xml') and filename != 'envelope.ini':
                shutil.copy(os.path.join(self._DIRS['RECEIVE_DIR'], self._medo_message_uid, filename),
                            os.path.join(self._DIRS['DELO_RECEIVE_DIR'], self._transport_guid, filename))

    def _create_delo_folder(self):
        if self._is_medo_document:
            # Документ
            if not os.path.exists(os.path.join(self._DIRS['DELO_RECEIVE_DIR'], self._transport_guid)):
                os.makedirs(os.path.join(self._DIRS['DELO_RECEIVE_DIR'], self._transport_guid))
        else:
            # Квитанция/Уведомление
            if not os.path.exists(os.path.join(self._DIRS['DELO_RECEIVE_DIR_REPORT'], self._transport_guid)):
                os.makedirs(os.path.join(self._DIRS['DELO_RECEIVE_DIR_REPORT'], self._transport_guid))

    def _save_delo_message(self):
        "Запись готового ДЕЛО сообщения в файл в директории приема ДЕЛО"
        if self._is_medo_document:
            # Создадим директорию для приёма готового сообщения Дело
            # Документ
            delo_doc_package_dir = os.path.join(self._DIRS['DELO_RECEIVE_DIR'], self._transport_guid)
            if not os.path.exists(delo_doc_package_dir):
                os.makedirs(delo_doc_package_dir)
            final_delo_xml = os.path.join(delo_doc_package_dir, 'DocInfo.xml')

        else:
            # Квитанция/Уведомление
            delo_report_package_dir = os.path.join(self._DIRS['DELO_RECEIVE_DIR_REPORT'], self._transport_guid)
            if not os.path.exists(delo_report_package_dir):
                os.makedirs(delo_report_package_dir)
            final_delo_xml = os.path.join(delo_report_package_dir, 'Report.xml')

        # Запишем готовое сообщение Дело в файл
        with open(final_delo_xml, 'wb') as f:
            self._delo_message_tree.write(f, pretty_print=True, encoding='utf-8')

    def _save_custom_medo_acknowledgment(self):
        if self._custom_ack_medo_result_tree:
            self._package_name = 'Acknowledgment_' + self._custom_ack_transport_guid
            if not os.path.exists(os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name)):
                os.makedirs(os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name))

            full_xml_filename = os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name, 'passport.xml')
            with open(full_xml_filename, 'wb') as f:
                self._custom_ack_medo_result_tree.write(f, pretty_print=True, encoding='utf-8')
            return True
        else:
            return False

    def _save_custom_medo_envelope(self):
        # annotation = ''
        # files_str = ''
        if self._custom_ack_medo_result_tree:
            annotation = 'Квитанция'
            files_str = '0=passport.xml'
        # Подготовим атрибуты для подстановки
        attrs = {
            'TITLE': annotation,
            'DATETIME': datetime.now().strftime('%d.%m.%Y %H:%M:%S'),
            'FILES': files_str
        }
        # Сформируем содержимое файла envelope.ini на основании шаблона
        with open(os.path.join(self._DIRS['TEMPLATES_DIR'], 'envelope.ini'), 'r', encoding='utf-8') as f:
            envelope_content = Template(f.read()).substitute(attrs)
        # Запишем файл envelope.ini

        with open(os.path.join(self._DIRS['MEDO_SEND_DIR'], self._package_name, 'envelope.ini'), 'w',
                  encoding='utf-8') as f:
            f.write(envelope_content)

    def _create_message(self):
        "Создание ДЕЛО сообщения из МЭДО Документа/Квитанции"

        medo_to_delo_result_tree = self._xml_work.xml_change_by_template(
            xml_source=self._medo_xml_filename_with_path,
            xslt_template=self._medo_to_delo_xslt_template_filename
        )

        if not self._is_medo_document:
            # Добавим недостающую дополнительную информацию
            if self._is_acknowledgment:
                transport_guid = self._medo_xml_tree.xpath('.//xdms:acknowledgment/@xdms:uid',
                                                           namespaces=self._namespaces_medo)
                message_info = self._message_broker.get_message_info_by_transport_uid(transport_guid[0])
            elif self._is_notification_medo:
                transport_guid = self._medo_xml_tree.xpath('.//xdms:notification/@xdms:uid',
                                                           namespaces=self._namespaces_medo)
                message_info = self._message_broker.get_message_info_by_document_uid(transport_guid[0])

            if message_info:
                # Получим сохраненные значения из message_info
                return_id = message_info[2]
                document_uid = message_info[5]
                document_uid = document_uid.replace("-", "")
                document_reg_number = message_info[3]
                document_reg_date = message_info[4]
                document_group = message_info[8]

                recipient_contact_new = etree.fromstring(message_info[6])
                sender_contact_new = etree.fromstring(message_info[7])

                # Найдем, куда записать сохраненные значения в создаваемом ДЕЛО сообщении

                sender_contact = medo_to_delo_result_tree.xpath('*/sev:Sender/sev:Contact',
                                                                namespaces=self._namespaces_delo)[0]
                # sender_contact_str = etree.tostring(sender_contact, encoding='unicode')
                recipient_contact = medo_to_delo_result_tree.xpath('*/sev:Recipient/sev:Contact',
                                                                   namespaces=self._namespaces_delo)[0]
                # recipient_contact_str = etree.tostring(recipient_contact, encoding='unicode')
                sender_contact.getparent().replace(sender_contact, sender_contact_new)
                recipient_contact.getparent().replace(recipient_contact, recipient_contact_new)

                if not self._is_acknowledgment:
                    # Уведомление
                    # authors = medo_to_delo_result_tree.xpath('*/sev:Document/sev:Author',
                    #                             namespaces=self._namespaces_delo)

                    if self._is_notification_confirm:
                        # document = medo_to_delo_result_tree.xpath('.//sev:DocumentList/sev:Document',
                        #                              namespaces=self._namespaces_delo)[0]
                        #
                        # authors = self._medo_xml_tree.xpath('*/sev:Document/sev:Author',
                        #                             namespaces=self._namespaces_delo)

                        notification_datetime = \
                            self._medo_xml_tree.xpath('.//xdms:notification/xdms:documentAccepted/xdms:time',
                                                      namespaces=self._namespaces_medo)[0].text
                        datetime_with_timezone = dateutil_parser.parse(notification_datetime).replace(
                            tzinfo=pytz.timezone('Etc/GMT-3')).isoformat()

                        attrs = {
                            'DATETIME': datetime_with_timezone,
                            'RETURN_UID': return_id,
                            'DOCUMENT_UID': document_uid,
                            'DOCUMENT_GROUP': document_group
                        }
                    if self._is_notification_refuse:
                        # notification_failure = tree.xpath('.//xdms:notification/xdms:documentRefused/xdms:reason',
                        #                                     namespaces={'xdms':"http://www.infpres.com/IEDMS"})[0].text
                        notification_datetime = self._medo_xml_tree.xpath(
                            './/xdms:notification/xdms:documentRefused/xdms:time',
                            namespaces=self._namespaces_medo)[0].text

                        notification_correspondent_name = self._medo_xml_tree.xpath(
                            './/xdms:notification/xdms:documentRefused/xdms:foundation/xdms:organization',
                            namespaces=self._namespaces_medo)[0].text

                        notification_reg_number = self._medo_xml_tree.xpath(
                            './/xdms:notification/xdms:documentRefused/xdms:foundation/xdms:num/xdms:number',
                            namespaces=self._namespaces_medo)[0].text
                        notification_reg_date = self._medo_xml_tree.xpath(
                            './/xdms:notification/xdms:documentRefused/xdms:foundation/xdms:num/xdms:date',
                            namespaces=self._namespaces_medo)[0].text

                        notification_reason = self._medo_xml_tree.xpath(
                            './/xdms:notification/xdms:documentRefused/xdms:reason',
                            namespaces=self._namespaces_medo)[0].text

                        datetime_with_timezone = dateutil_parser.parse(notification_datetime).replace(
                            tzinfo=pytz.timezone('Etc/GMT-3')).isoformat()

                        attrs = {
                            'DATETIME': datetime_with_timezone,
                            'RETURN_UID': return_id,
                            'DOCUMENT_UID': document_uid,
                            'DOCUMENT_GROUP': document_group
                        }

                else:
                    # Квитанция
                    # Определим значение дополнительных атрибутов, которые будут подставлены в Дело уведомление
                    # Дата и время формирования сообщения

                    acknowledgment_datetime = self._medo_xml_tree.xpath('.//xdms:acknowledgment/xdms:time',
                                                                        namespaces=self._namespaces_medo)[0].text
                    datetime_with_timezone = dateutil_parser.parse(acknowledgment_datetime).replace(
                        tzinfo=pytz.timezone('Etc/GMT-3')).isoformat()

                    # acknowledgment_number = self._medo_xml_tree.xpath('.//xdms:acknowledgment/@xdms:uid',
                    #                                    namespaces=self._namespaces_medo)[0]

                    attrs = {
                        'REG_DATE': document_reg_date,
                        'REG_NUMBER': document_reg_number,
                        'DATETIME': datetime_with_timezone,
                        'RETURN_UID': return_id,
                        'DOCUMENT_UID': document_uid,
                        'DOCUMENT_GROUP': document_group
                    }

            else:
                logger.write(
                    f"Квитанция/уведомление для пакета {self._medo_message_uid} не может быть обработана(о). "
                    f"Информация по пакету не найдена в базе "
                )

                self._move_medo_message_to_error()

                return 0
        else:
            # Документ
            # transport_guid = self._medo_xml_tree.xpath('.//xdms:header/@xdms:uid', namespaces=self._namespaces_medo)
            # government_guid = self._medo_xml_tree.xpath('.//xdms:header/xdms:source/@xdms:uid',
            #                              namespaces=self._namespaces_medo)

            # organization_name = tree.xpath('.//xdms:document/xdms:addressees/xdms:addressee/xdms:organization', namespaces={'xdms':"http://www.infpres.com/IEDMS"})[0].text
            organization_name = self._medo_xml_tree.xpath(
                '/xdms:communication/xdms:deliveryIndex/xdms:destination/xdms:destination/xdms:organization',
                namespaces=self._namespaces_medo)[0].text
            organization_uid = self._message_broker.get_address_uid(organization_name)
            organization_deloname = self._message_broker.get_address_deloname(organization_name)
            organization_docgroup = self._message_broker.get_address_docgroup(organization_name)

            if organization_uid == None:
                return None
            # Документ
            # Определим значение дополнительных атрибутов, которые будут подставлены в МЭДО сообщение
            attrs = {
                'DOCUMENT_UID': str(uuid.uuid4()).replace('-', ''),
                'RETURN_UID': str(uuid.uuid4()).replace('-', ''),
                'TRANSPORT_UID': str(uuid.uuid4()).replace('-', ''),
                'TIME': datetime.now().replace(microsecond=0).isoformat(),
                'FILE_UID': str(uuid.uuid4()).replace('-', ''),
                'ORGANIZATION_UID': organization_uid,
                'ORGANIZATION_DELONAME': organization_deloname,
                'ORGANIZATION_DOCGROUP': organization_docgroup
            }

        xml_template = Template(str(medo_to_delo_result_tree))
        medo_to_delo_result_tree = xml_template.substitute(attrs)
        medo_to_delo_result_tree = etree.fromstring(medo_to_delo_result_tree)
        medo_to_delo_result_tree = medo_to_delo_result_tree.getroottree()

        # Загрузим XSD схему для проверки ДЕЛО сообщения
        xmlschema_doc = etree.parse(self._xsd_filename)
        xmlschema = etree.XMLSchema(xmlschema_doc)
        # Проверим ДЕЛО сообщение по XSD схеме
        if xmlschema.validate(medo_to_delo_result_tree):
            # Проверка прошла успешно, можно сохранять сообщение
            self._transport_guid = attrs['RETURN_UID']
            self._delo_message_tree = medo_to_delo_result_tree

            # Если установлена опция формирования квитанций о получении пакета демоном, то создадим эту квитанцию
            if self._is_medo_document:
                if self._message_broker._is_custom_medo_acknowledgment:
                    self._custom_ack_medo_result_tree = self._create_custom_medo_acknowledgment()
                xdms_uid = self._medo_xml_tree.xpath('/xdms:communication/xdms:header/@xdms:uid',
                                                     namespaces=self._namespaces_medo)[0]
                # xdms_id = self._message_uid[self._message_uid[:self._message_uid.rfind("_")].rfind("_") + 1:self._message_uid.rfind("_")] #достаем document id из имени пакета
                document_uid = self._medo_xml_tree.xpath('/xdms:communication/xdms:document/@xdms:uid',
                                                         namespaces=self._namespaces_medo)[0]
                remote_reg_number = self._medo_xml_tree.xpath('.//xdms:document/xdms:num/xdms:number',
                                                              namespaces=self._namespaces_medo)[0].text
                kwargs = {
                    'return_id': xdms_uid,
                    'document_id': document_uid,
                    'remote_reg_number': remote_reg_number
                }
                self._message_broker.set_message_info(self._transport_guid, "medo", **kwargs)
            else:
                if not self._is_acknowledgment:
                    if self._is_notification_refuse != None and self._is_notification_refuse != False:
                        refuse_text = f"Поступил отказ в регистрации на документ, отправленный по СЭВ. " \
                                      f"Корреспондент: {notification_correspondent_name}. " \
                                      f"Регистрационный номер: {notification_reg_number}. " \
                                      f"Дата регистрации: {notification_reg_date}. " \
                                      f"Причина: {notification_reason}. "
                        logger.write(refuse_text)
                        mailer = Mailer(notification_reg_number, refuse_text)
                        mailer._send_mail_to_clerks()

            return 'Success'
        else:
            # Проверка не прошла, выведем причину непрохождения
            try:
                xmlschema.assertValid(medo_to_delo_result_tree)
            except Exception as e:
                logger.write(e)

            return None

    def _create_custom_medo_acknowledgment(self):

        custom_medo_ack_result_tree_xml = self._xml_work.xml_change_by_template(
            xml_source=self._medo_xml_filename_with_path,
            xslt_template=self._xslt_template_filename_for_custom_acknowledgment
        )

        source_organization_name = self._medo_xml_tree.xpath(
            '/xdms:communication/xdms:deliveryIndex/xdms:destination/xdms:destination/xdms:organization',
            namespaces={'xdms': "http://www.infpres.com/IEDMS"})[0].text

        source_organization_uid = self._message_broker.get_address_uid(source_organization_name)
        document_uid = \
            self._medo_xml_tree.xpath('.//xdms:header/@xdms:uid', namespaces={'xdms': "http://www.infpres.com/IEDMS"})[
                0]

        if source_organization_uid == None:
            return None
        # Документ
        # Определим значение дополнительных атрибутов, которые будут подставлены в МЭДО сообщение

        self._custom_ack_transport_guid = str(uuid.uuid4())

        attrs = {
            'MESSAGE_UID': self._custom_ack_transport_guid,
            'DATETIME': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
            'SOURCE_GUID': source_organization_uid,
            'ORGANIZATION_NAME': source_organization_name,
            'DOCUMENT_UID': document_uid
        }

        xml_template = Template(str(custom_medo_ack_result_tree_xml)).substitute(attrs)
        custom_medo_ack_result_tree_xml = etree.fromstring(xml_template).getroottree()

        # Загрузим XSD схему для проверки ДЕЛО сообщения
        xmlschema_doc = etree.parse(self._xsd_acknowledgment_filename)
        xmlschema = etree.XMLSchema(xmlschema_doc)
        # Проверим ДЕЛО сообщение по XSD схеме
        if xmlschema.validate(custom_medo_ack_result_tree_xml):
            return custom_medo_ack_result_tree_xml
        else:
            # Проверка не прошла, выведем причину непрохождения
            try:
                xmlschema.assertValid(custom_medo_ack_result_tree_xml)
            except Exception as e:
                logger.write(e)
            return None

    def receive(self):
        if not self._is_ignore:
            result = self._create_message()
            if result:
                # Создадим директорию для приёма Дело сообщения
                self._create_delo_folder()
                # Скопируем в директорию МЭДО сообщения приложенные файлы ДЕЛО сообщения
                self._copy_applied_files()
                # Сохраним сообщение в директорию отправки ДЕЛО
                self._save_delo_message()
                # Создадим в директории ДЕЛО сообщения файл .env
                self._create_delo_env_file()

                if self._is_medo_document:
                    if self._message_broker._is_custom_medo_acknowledgment:
                        self._save_custom_medo_acknowledgment()
                        self._save_custom_medo_envelope()
                # Переместим сообщение МЭДО в архивную папку, чтобы не отправлять его еще раз
                self._move_medo_message_to_archive()
                if not self._already_exist:
                    logger.write(f"Пакет {self._medo_message_uid} успешно принят")
        else:
            self._move_medo_message_to_error()
            logger.write(f"Внимание. Уведомление {self._medo_message_uid} игнорировано. ")
            # f"Перемещено в директорию {os.path.join(self._DIRS['ERRORS_MEDO'], self._datestamp)}")


class Daemon:

    def __init__(self):
        self._config_parser = ConfParser()
        if self._config_parser:
            self._time_interval = self._config_parser.config.getint('Daemon', 'time_interval')

        self._message_broker = MessageBroker()

    def run(self):
        # Запуск Демона
        while True:
            self._message_broker.run()
            time.sleep(self._time_interval)

    # def run(self):
    #     # Запуск Демона
    #     self._message_broker.run()

if __name__ == '__main__':
    Daemon().run()

# if __name__ == '__main__':
#     Daemon().run()
#     schedule.every(1).minutes.at(":00").do(Daemon().run)
#     while True:
#         schedule.run_pending()
#         time.sleep(60)

