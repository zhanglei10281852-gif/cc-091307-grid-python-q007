"""联系方式脱敏：非本片区查询时隐藏手机号与证件号中段。"""


def mask_phone(phone):
    """13812345678 -> 138****5678"""
    if not phone:
        return phone
    phone = str(phone)
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "*" * len(phone)


def mask_id_card(id_card):
    """11010119900307771X -> 1101************1X"""
    if not id_card:
        return id_card
    id_card = str(id_card)
    if len(id_card) > 6:
        return id_card[:4] + "*" * (len(id_card) - 6) + id_card[-2:]
    return id_card[0] + "*" * (len(id_card) - 1)
