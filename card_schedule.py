"""Single calendar rule for card due dates; invoice period stays independent of offsets."""
import calendar
from datetime import date, timedelta


def due_day(card):
    value=card.get('payment_due_day') or card.get('monthly_due_day') or card.get('due_day')
    if value is None and card.get('payment_due_on'):
        value=date.fromisoformat(card['payment_due_on']).day
    if isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=31:
        raise ValueError('Kartın son ödeme gününü Profil ve Ayarlar bölümünde 1–31 olarak girin.')
    return value


def offset_value(value):
    if isinstance(value,str) and value.lstrip('-+').isdigit():value=int(value)
    if isinstance(value,bool) or not isinstance(value,int) or not -5<=value<=5:
        raise ValueError('Aylık vade düzeltmesi −5 ile +5 arasında tam gün olmalı.')
    return value


def due_on(card,month,offset=0):
    first=date.fromisoformat(month+'-01')
    base=first.replace(day=min(due_day(card),calendar.monthrange(first.year,first.month)[1]))
    return (base+timedelta(days=offset_value(offset))).isoformat()


def next_due(card,on):
    month=on.strftime('%Y-%m')
    result=due_on(card,month)
    if result<on.isoformat():
        index=on.year*12+on.month
        year,zero_month=divmod(index,12)
        result=due_on(card,f'{year:04}-{zero_month+1:02}')
    return result
