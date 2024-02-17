from datetime import datetime, timedelta, time
from uuid import uuid4  # Номера расписаний должны быть уникальными во времени и пространстве
from threading import Thread, Event  # Поток и событие остановки потока получения новых бар по расписанию биржи
import os.path
import csv
import logging

from backtrader.feed import AbstractDataBase
from backtrader.utils.py3 import with_metaclass
from backtrader import TimeFrame, date2num

from BackTraderAlor import ALStore


class MetaALData(AbstractDataBase.__class__):
    def __init__(self, name, bases, dct):
        super(MetaALData, self).__init__(name, bases, dct)  # Инициализируем класс данных
        ALStore.DataCls = self  # Регистрируем класс данных в хранилище Алор


class ALData(with_metaclass(MetaALData, AbstractDataBase)):
    """Данные Алор"""
    params = (
        ('provider_name', None),  # Название провайдера. Если не задано, то первое название по ключу name
        ('four_price_doji', False),  # False - не пропускать дожи 4-х цен, True - пропускать
        ('schedule', None),  # Расписание работы биржи. Если не задано, то берем из подписки
        ('live_bars', False),  # False - только история, True - история и новые бары
    )
    datapath = os.path.join('..', '..', 'Data', 'Alor', '')  # Путь до файла истории
    delimiter = '\t'  # Разделитель значений в файле истории. По умолчанию табуляция
    dt_format = '%d.%m.%Y %H:%M'  # Формат представления даты и времени в файле истории. По умолчанию русский формат

    def islive(self):
        """Если подаем новые бары, то Cerebro не будет запускать preload и runonce, т.к. новые бары должны идти один за другим"""
        return self.p.live_bars

    def __init__(self, **kwargs):
        self.store = ALStore(**kwargs)  # Передаем параметры в хранилище Алор. Может работать самостоятельно, не через хранилище
        self.alor_timeframe = self.bt_timeframe_to_alor_timeframe(self.p.timeframe, self.p.compression)  # Конвертируем временной интервал из BackTrader в Алор
        self.tf = self.bt_timeframe_to_tf(self.p.timeframe, self.p.compression)  # Конвертируем временной интервал из BackTrader для имени файла истории и расписания
        self.intraday = self.p.timeframe in (TimeFrame.Minutes, TimeFrame.Seconds)  # Внутридневной временной интервал. Алор измеряет внутридневные интервалы в секундах
        self.board, self.symbol = self.store.provider.dataname_to_board_symbol(self.p.dataname)  # По тикеру получаем код режима торгов и тикера
        self.file = f'{self.board}.{self.symbol}_{self.tf}'  # Имя файла истории
        self.logger = logging.getLogger(f'ALData.{self.file}')  # Будем вести лог
        self.file_name = f'{self.datapath}{self.file}.txt'  # Полное имя файла истории
        self.exchange = self.store.provider.get_exchange(self.board, self.symbol)  # Биржа тикера. В Алор запросы выполняются по коду биржи и тикера
        self.history_bars = []  # Исторические бары из файла и истории после проверки на соответствие условиям выборки
        self.guid = None  # Идентификатор подписки/расписания на историю цен
        self.exit_event = Event()  # Определяем событие выхода из потока
        self.dt_last_open = datetime.min  # Дата и время открытия последнего полученного бара
        self.last_bar_received = False  # Получен последний бар
        self.live_mode = False  # Режим получения бар. False = История, True = Новые бары

    def setenvironment(self, env):
        """Добавление хранилища Алор в cerebro"""
        super(ALData, self).setenvironment(env)
        env.addstore(self.store)  # Добавление хранилища Алор в cerebro

    def start(self):
        super(ALData, self).start()
        self.put_notification(self.DELAYED)  # Отправляем уведомление об отправке исторических (не новых) бар
        self.get_bars_from_file()  # Получаем бары из файла
        self.get_bars_from_history()  # Получаем бары из истории
        if len(self.history_bars) > 0:  # Если был получен хотя бы 1 бар
            self.put_notification(self.CONNECTED)  # то отправляем уведомление о подключении и начале получения исторических бар
        if self.p.live_bars:  # Если получаем историю и новые бары
            if self.p.schedule:  # Если получаем новые бары по расписанию
                self.guid = uuid4().hex  # guid расписания
                Thread(target=self.stream_bars).start()  # Создаем и запускаем получение новых бар по расписанию в потоке
            else:  # Если получаем новые бары по подписке
                # Ответ ALOR OpenAPI Support: Чтобы получать последний бар сессии на первом тике следующей сессии, нужно использовать скрытый параметр frequency в ms с очень большим значением (1_000_000_000)
                # С 09:00 до 10:00 Алор перезапускает сервер, и подписка на последний бар предыдущей сессии по фьючерсам пропадает.
                # В этом случае нужно брать данные не из подписки, а из расписания
                seconds_from = self.get_seconds_from()  # Дата и время начала выборки
                self.logger.debug(f'Запуск подписки на новые бары с {self.store.provider.utc_timestamp_to_msk_datetime(seconds_from).strftime(self.dt_format)}')
                self.guid = self.store.provider.bars_get_and_subscribe(self.exchange, self.symbol, self.alor_timeframe, seconds_from, 1_000_000_000)  # Подписываемся на бары, получаем guid подписки
                self.logger.debug(f'Код подписки {self.guid}')

    def _load(self):
        """Загрузка бара из истории или нового бара"""
        if not self.p.live_bars:  # Если получаем только историю (self.history_bars)
            if len(self.history_bars) == 0:  # Если исторических данных нет / Все исторические данные получены
                self.put_notification(self.DISCONNECTED)  # Отправляем уведомление об окончании получения исторических бар
                self.logger.debug('Бары из файла/истории отправлены в ТС. Новые бары получать не нужно. Выход')
                return False  # Больше сюда заходить не будем
            bar = self.history_bars[0]  # Берем первый бар из выборки, с ним будем работать
            self.history_bars.remove(bar)  # Убираем его из хранилища новых бар
        else:  # Если получаем историю и новые бары (self.store.new_bars)
            if len(self.store.new_bars) == 0:  # Если в хранилище никаких новых бар нет
                return None  # то нового бара нет, будем заходить еще
            new_bars = [b for b in self.store.new_bars if b['guid'] == self.guid]  # Смотрим в хранилище новых бар бары с guid подписки
            if len(new_bars) == 0:  # Если новый бар еще не появился
                self.logger.debug('Новых бар нет')
                return None  # то нового бара нет, будем заходить еще
            self.last_bar_received = len(new_bars) == 1  # Если в хранилище остался 1 бар, то мы будем получать последний возможный бар
            if self.last_bar_received:  # Получаем последний возможный бар
                self.logger.debug('Получение последнего возможного на данный момент бара')
            new_bar = new_bars[0]  # Берем первый бар из хранилища
            self.store.new_bars.remove(new_bar)  # Убираем его из хранилища
            new_bar = new_bar['data']  # С данными этого бара будем работать
            dt_open = self.get_bar_open_date_time(new_bar['time'])  # Дата и время открытия бара
            bar = dict(datetime=dt_open,
                       open=new_bar['open'], high=new_bar['high'], low=new_bar['low'], close=new_bar['close'],
                       volume=new_bar['volume'])  # Бар из хранилища новых бар
            if not self.is_bar_valid(bar):  # Если бар не соответствует всем условиям выборки
                return None  # то пропускаем бар, будем заходить еще
            self.logger.debug(f'Сохранение нового бара с {bar["datetime"].strftime(self.dt_format)} в файл')
            self.save_bar_to_file(bar)  # Сохраняем бар в конец файла
            if self.last_bar_received and not self.live_mode:  # Если получили последний бар и еще не находимся в режиме получения новых бар (LIVE)
                self.put_notification(self.LIVE)  # Отправляем уведомление о получении новых бар
                self.live_mode = True  # Переходим в режим получения новых бар (LIVE)
            elif self.live_mode and not self.last_bar_received:  # Если находимся в режиме получения новых бар (LIVE)
                self.put_notification(self.DELAYED)  # Отправляем уведомление об отправке исторических (не новых) бар
                self.live_mode = False  # Переходим в режим получения истории
        # Все проверки пройдены. Записываем полученный исторический/новый бар
        self.lines.datetime[0] = date2num(bar['datetime'])  # DateTime
        self.lines.open[0] = self.store.provider.alor_price_to_price(self.exchange, self.symbol, bar['open'])  # Open
        self.lines.high[0] = self.store.provider.alor_price_to_price(self.exchange, self.symbol, bar['high'])  # High
        self.lines.low[0] = self.store.provider.alor_price_to_price(self.exchange, self.symbol, bar['low'])  # Low
        self.lines.close[0] = self.store.provider.alor_price_to_price(self.exchange, self.symbol, bar['close'])  # Close
        self.lines.volume[0] = bar['volume']  # Volume
        self.lines.openinterest[0] = 0  # Открытый интерес в Алор не учитывается
        return True  # Будем заходить сюда еще

    def stop(self):
        super(ALData, self).stop()
        if self.guid is not None:  # Если была подписка/расписание
            if self.p.schedule:  # Если получаем новые бары по расписанию
                self.exit_event.set()  # то отменяем расписание
            else:  # Если получаем новые бары по подписке
                self.logger.info(f'Отмена подписки {self.guid} на новые бары')
                self.store.provider.unsubscribe(self.guid)  # то отменяем подписку
            self.put_notification(self.DISCONNECTED)  # Отправляем уведомление об окончании получения новых бар
        self.store.DataCls = None  # Удаляем класс данных в хранилище

    # Получение бар

    def get_bars_from_file(self) -> None:
        """Получение бар из файла"""
        self.logger.debug(f'Получение бар из файла {self.file_name}')
        if not os.path.isfile(self.file_name):  # Если файл не существует
            self.logger.warning(f'Файл {self.file_name} не найден и будет создан')
            return  # то выходим, дальше не продолжаем
        with open(self.file_name) as file:  # Открываем файл на последовательное чтение
            reader = csv.reader(file, delimiter=self.delimiter)  # Данные в строке разделены табуляцией
            next(reader, None)  # Пропускаем первую строку с заголовками
            for csv_row in reader:  # Последовательно получаем все строки файла
                bar = dict(datetime=datetime.strptime(csv_row[0], self.dt_format),
                           open=float(csv_row[1]), high=float(csv_row[2]), low=float(csv_row[3]), close=float(csv_row[4]),
                           volume=int(csv_row[5]))  # Бар из файла
                if self.is_bar_valid(bar):  # Если исторический бар соответствует всем условиям выборки
                    self.history_bars.append(bar)  # то добавляем бар
        if len(self.history_bars) > 0:  # Если были получены бары из файла
            self.logger.debug(f'Получено бар из файла: {len(self.history_bars)} с {self.history_bars[0]["datetime"].strftime(self.dt_format)} по {self.history_bars[-1]["datetime"].strftime(self.dt_format)}')

    def get_bars_from_history(self) -> None:
        """Получение бар из истории"""
        file_history_bars_len = len(self.history_bars)  # Кол-во полученных бар из файла для лога
        seconds_from = self.get_seconds_from()  # Дата и время начала выборки
        self.logger.debug(f'Получение бар из истории с {self.store.provider.utc_timestamp_to_msk_datetime(seconds_from).strftime(self.dt_format)}')
        seconds_to = self.store.provider.msk_datetime_to_utc_timestamp(self.p.todate) if self.p.todate else 32536799999  # Дата и время окончания выборки
        history_bars = self.store.provider.get_history(self.exchange, self.symbol, self.alor_timeframe, seconds_from, seconds_to)  # Получаем бары из Алор
        if not history_bars or 'history' not in history_bars:  # Если бары не получены
            self.logger.error(f'Ошибка при получении бар из истории. exchange={self.exchange}, symbol={self.symbol}, timeframe={self.alor_timeframe}, seconds_from={seconds_from}, seconds_to={seconds_to}')
            return  # то выходим, дальше не продолжаем
        history_bars = history_bars['history']  # Полученные бары истории
        for history_bar in history_bars:  # Пробегаемся по всем полученным барам
            bar = dict(datetime=self.get_bar_open_date_time(history_bar['time']),
                       open=history_bar['open'], high=history_bar['high'], low=history_bar['low'], close=history_bar['close'],
                       volume=history_bar['volume'])  # Бар из истории
            if self.is_bar_valid(bar):  # Если исторический бар соответствует всем условиям выборки
                self.history_bars.append(bar)  # то добавляем бар
                self.save_bar_to_file(bar)  # и сохраняем его в файл
        if len(self.history_bars) - file_history_bars_len > 0:  # Если получены бары из истории
            self.logger.debug(f'Получено бар из истории: {len(self.history_bars) - file_history_bars_len} с {self.history_bars[file_history_bars_len]["datetime"].strftime(self.dt_format)} по {self.history_bars[-1]["datetime"].strftime(self.dt_format)}')
        else:  # Бары из истории не получены
            self.logger.debug('Из истории новых бар не получено')

    def stream_bars(self) -> None:
        """Поток получения новых бар по расписанию биржи"""
        self.logger.debug('Запуск получения новых бар по расписанию')
        while True:
            market_datetime_now = self.p.schedule.utc_to_msk_datetime(datetime.utcnow())  # Текущее время на бирже
            trade_bar_open_datetime = self.p.schedule.trade_bar_open_datetime(market_datetime_now, self.tf)  # Дата и время открытия бара, который будем получать
            trade_bar_request_datetime = self.p.schedule.trade_bar_request_datetime(market_datetime_now, self.tf)  # Дата и время запроса бара на бирже
            sleep_time_secs = (trade_bar_request_datetime - market_datetime_now).total_seconds()  # Время ожидания в секундах
            self.logger.debug(f'Получение новых бар с {trade_bar_open_datetime.strftime(self.dt_format)} по расписанию в {trade_bar_request_datetime.strftime(self.dt_format)}. Ожидание {sleep_time_secs} с')
            exit_event_set = self.exit_event.wait(sleep_time_secs)  # Ждем нового бара или события выхода из потока
            if exit_event_set:  # Если произошло событие выхода из потока
                self.logger.warning('Отмена получения новых бар по расписанию')
                return  # Выходим из потока, дальше не продолжаем
            self.logger.debug(f'Получение новых бар по расписанию с {trade_bar_open_datetime.strftime(self.dt_format)}')
            seconds_from = self.p.schedule.msk_datetime_to_utc_timestamp(trade_bar_open_datetime)  # Дата и время бара в timestamp UTC
            bars = self.store.provider.get_history(self.exchange, self.symbol, self.alor_timeframe, seconds_from)  # Получаем ответ на запрос истории рынка
            if not bars:  # Если ничего не получили
                self.logger.warning('По расписанию ничего не получено')
                continue  # то будем получать следующий бар
            if 'history' not in bars:  # Если не получили историю
                self.logger.warning('По расписанию бары не получены')
                continue  # то будем получать следующий бар
            bars = bars['history']  # Последний сформированный и текущий несформированный (если имеется) бары
            if len(bars) == 0:  # Если бары не получены
                self.logger.warning('По расписанию новые бары не получены')
                continue  # то будем получать следующий бар
            bar = bars[0]  # Получаем первый (завершенный) бар
            self.logger.debug('Получен бар по расписанию')
            self.store.new_bars.append(dict(guid=self.guid, data=bar))  # Обработчик новых бар по подписке из Алор

    # Функции

    @staticmethod
    def bt_timeframe_to_alor_timeframe(timeframe, compression=1) -> str:
        """Перевод временнОго интервала из BackTrader в Алор

        :param TimeFrame timeframe: Временной интервал
        :param int compression: Размер временнОго интервала
        :return: Временной интервал Алор
        """
        if timeframe == TimeFrame.Days:  # Дневной временной интервал (по умолчанию)
            return 'D'
        elif timeframe == TimeFrame.Weeks:  # Недельный временной интервал
            return 'W'
        elif timeframe == TimeFrame.Months:  # Месячный временной интервал
            return 'M'
        elif timeframe == TimeFrame.Years:  # Годовой временной интервал
            return 'Y'
        elif timeframe == TimeFrame.Minutes:  # Минутный временной интервал
            return str(compression * 60)  # Переводим в секунды
        elif timeframe == TimeFrame.Seconds:  # Секундный временной интервал
            return str(compression)  # Оставляем в секундах

    @staticmethod
    def bt_timeframe_to_tf(timeframe, compression=1) -> str:
        """Перевод временнОго интервала из BackTrader для имени файла истории и расписания https://ru.wikipedia.org/wiki/Таймфрейм

        :param TimeFrame timeframe: Временной интервал
        :param int compression: Размер временнОго интервала
        :return: Временной интервал для имени файла истории и расписания
        """
        if timeframe == TimeFrame.Minutes:  # Минутный временной интервал
            return f'M{compression}'
        # Часовой график f'H{compression}' заменяем минутным. Пример: H1 = M60
        elif timeframe == TimeFrame.Days:  # Дневной временной интервал
            return f'D1'
        elif timeframe == TimeFrame.Weeks:  # Недельный временной интервал
            return f'W1'
        elif timeframe == TimeFrame.Months:  # Месячный временной интервал
            return f'MN1'
        elif timeframe == TimeFrame.Years:  # Годовой временной интервал
            return f'Y1'
        raise NotImplementedError  # С остальными временнЫми интервалами не работаем

    def get_seconds_from(self) -> int:
        """Дата и время начала выборки в кол-ве секунд, прошедших с 01.01.1970 00:00 UTC"""
        if len(self.history_bars):  # Если получили бары из файла
            dt = self.get_bar_close_date_time(self.history_bars[-1]['datetime'])  # то время начала выборки смещаем на следующий бар по UTC
        elif self.p.fromdate:  # Если бары из файла не получили, но заданы дата и время начала интервала
            dt = self.p.fromdate  # то время начала выборки берем из даты и времени начала интервала
        else:  # Если бар из файла нет и не заданы дата и время начала интервала
            return 0  # то время начала выборки берем минимально возможное
        return self.store.provider.msk_datetime_to_utc_timestamp(dt)

    def get_bar_open_date_time(self, timestamp) -> datetime:
        """Дата и время открытия бара. Переводим из GMT в MSK для интрадея. Оставляем в GMT для дневок и выше."""
        return self.store.provider.utc_timestamp_to_msk_datetime(timestamp) if self.intraday\
            else datetime.utcfromtimestamp(timestamp)  # Время открытия бара

    def get_bar_close_date_time(self, dt_open, period=1) -> datetime:
        """Дата и время закрытия бара"""
        if self.p.timeframe == TimeFrame.Days:  # Дневной временной интервал (по умолчанию)
            return dt_open + timedelta(days=period)  # Время закрытия бара
        elif self.p.timeframe == TimeFrame.Weeks:  # Недельный временной интервал
            return dt_open + timedelta(weeks=period)  # Время закрытия бара
        elif self.p.timeframe == TimeFrame.Months:  # Месячный временной интервал
            year = dt_open.year + (dt_open.month + period - 1) // 12  # Год
            month = (dt_open.month + period - 1) % 12 + 1  # Месяц
            return datetime(year, month, 1)  # Время закрытия бара
        elif self.p.timeframe == TimeFrame.Years:  # Годовой временной интервал
            return dt_open.replace(year=dt_open.year + period)  # Время закрытия бара
        elif self.p.timeframe == TimeFrame.Minutes:  # Минутный временной интервал
            return dt_open + timedelta(minutes=self.p.compression * period)  # Время закрытия бара
        elif self.p.timeframe == TimeFrame.Seconds:  # Секундный временной интервал
            return dt_open + timedelta(seconds=self.p.compression * period)  # Время закрытия бара

    def is_bar_valid(self, bar) -> bool:
        """Проверка бара на соответствие условиям выборки"""
        dt_open = bar['datetime']  # Дата и время открытия бара МСК
        if dt_open <= self.dt_last_open:  # Если пришел бар из прошлого (дата открытия меньше последней даты открытия)
            self.logger.debug(f'Дата/время открытия бара {dt_open} <= последней даты/времени открытия {self.dt_last_open}')
            return False  # то бар не соответствует условиям выборки
        if self.p.fromdate and dt_open < self.p.fromdate or self.p.todate and dt_open > self.p.todate:  # Если задан диапазон, а бар за его границами
            self.logger.debug(f'Дата/время открытия бара {dt_open} за границами диапазона {self.p.fromdate} - {self.p.todate}')
            return False  # то бар не соответствует условиям выборки
        if self.p.sessionstart != time.min and dt_open.time() < self.p.sessionstart:  # Если задано время начала сессии и открытие бара до этого времени
            self.logger.debug(f'Дата/время открытия бара {dt_open} до начала торговой сессии {self.p.sessionstart}')
            return False  # то бар не соответствует условиям выборки
        dt_close = self.get_bar_close_date_time(dt_open)  # Дата и время закрытия бара
        if self.p.sessionend != time(23, 59, 59, 999990) and dt_close.time() > self.p.sessionend:  # Если задано время окончания сессии и закрытие бара после этого времени
            self.logger.debug(f'Дата/время открытия бара {dt_open} после окончания торговой сессии {self.p.sessionend}')
            return False  # то бар не соответствует условиям выборки
        if not self.p.four_price_doji and bar['high'] == bar['low']:  # Если не пропускаем дожи 4-х цен, но такой бар пришел
            self.logger.debug(f'Бар {dt_open} - дожи 4-х цен')
            return False  # то бар не соответствует условиям выборки
        time_market_now = self.get_alor_date_time_now()  # Текущее биржевое время
        if dt_close > time_market_now and time_market_now.time() < self.p.sessionend:  # Если время закрытия бара еще не наступило на бирже, и сессия еще не закончилась
            self.logger.debug(f'Дата/время {dt_close} закрытия бара еще не наступило')
            return False  # то бар не соответствует условиям выборки
        self.dt_last_open = dt_open  # Запоминаем дату/время открытия пришедшего бара для будущих сравнений
        return True  # В остальных случаях бар соответствуем условиям выборки

    def save_bar_to_file(self, bar) -> None:
        """Сохранение бара в конец файла"""
        if not os.path.isfile(self.file_name):  # Существует ли файл
            self.logger.warning(f'Файл {self.file_name} не найден и будет создан')
            with open(self.file_name, 'w', newline='') as file:  # Создаем файл
                writer = csv.writer(file, delimiter=self.delimiter)  # Данные в строке разделены табуляцией
                writer.writerow(bar.keys())  # Записываем заголовок в файл
        with open(self.file_name, 'a', newline='') as file:  # Открываем файл на добавление в конец. Ставим newline, чтобы в Windows не создавались пустые строки в файле
            writer = csv.writer(file, delimiter=self.delimiter)  # Данные в строке разделены табуляцией
            csv_row = bar.copy()  # Копируем бар для того, чтобы изменить формат даты
            csv_row['datetime'] = csv_row['datetime'].strftime(self.dt_format)  # Приводим дату к формату файла
            writer.writerow(csv_row.values())  # Записываем бар в конец файла
            self.logger.debug(f'В файл {self.file_name} записан бар на {csv_row["datetime"]}')

    def get_alor_date_time_now(self) -> datetime:
        """Текущая дата и время
        - Если получили последний бар истории, то запрашием текущие дату и время с сервера Алор
        - Если находимся в режиме получения истории, то переводим текущие дату и время с компьютера в МСК
        """
        return self.store.provider.utc_timestamp_to_msk_datetime(self.store.provider.get_time()) if self.last_bar_received\
            else datetime.now(self.store.provider.tz_msk).replace(tzinfo=None)
