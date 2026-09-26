"""archive/pdftext.py: facts from the platform's notices and certificates.

The fixtures reproduce what pypdf returns for the real documents — sentences
broken across lines, digits and CJK padded with spaces — with made-up names.
"""

from archive import pdftext

CORRECTION = (
    "浙江省数据知识产权登记\n形式审查补正通知书\n \n  甲科技有限公司  ：\n"
    "您于 2026 年 8 月 28 日提交的 某设备运行日度统计\n分析数据 数据知识产权登记申请，经形式审查，不符合《浙江省数据\n"
    "知识产权登记办法(试行)》第(四)条及第(五)条规定:\n□ 登记申请表填写不符合要求\n□ 证明文件不符合要求\n"
    "■ 针对 算法规则、数据样例 需要作出补充说明\n请在十个工作日内进行  ■ 补正修改 ■ 说明\n"
    " 2026 年 10 月 14 日前无正当理由逾期不答复的，视为撤回登记\n申请。\n"
    "备注:算法规则与数据样例不适应，样例数据中“等级”的获取过程在算法规则中没有说\n明。\n"
    "工作时间联系咨询:0571-00000000\n浙江省数据知识产权登记中心\n 2026 年 9 月 23 日"
)

CORRECTION_FORM_ONLY = (
    "浙江省数据知识产权登记\n形式审查补正通知书\n \n  甲科技有限公司  ：\n"
    "您于 2026 年 8 月 31 日提交的 某气体专项\n分析数据 数据知识产权登记申请，经形式审查，不符合规定:\n"
    "■ 登记申请表填写不符合要求\n□ 证明文件不符合要求\n□ 针对 需要作出补充说明\n"
    "请在十个工作日内进行  ■ 补正修改 □ 说明\n 2026 年 10 月 13 日前无正当理由逾期不答复的，视为撤回登记\n申请。\n"
    "备注:算法规则与应用场景中不宜出现固定数据量或统计时间（数\n据可动态更新）；\n"
    "工作时间联系咨询:0573-00000000\n浙江省数据知识产权登记中心\n 2026 年 9 月 22 日"
)

ANNOUNCEMENT = (
    "浙江省数据知识产权登记\n登记公告通知书\n \n  甲科技有限公司  ：\n"
    "根据《浙江省数据知识产权登记办法(试行)》第(八)条及第(十一\n)条规定，您于 2026 年 5 月 28 日提交的( 某港区出运窗口安全评\n"
    "估数据 )数据知识产权登记申请，符合发证公告条件\n，予以公告。登记平台颁发的数据知识产权登记电子证书有效期，自\n"
    "2026 年 8 月 8 日至 2029 年 8 月 8 日\n登记证书有效期满，需要继续使用证书的，申请人应当在期满前六\n"
    "个月内按照规定办理续展登记手续。\n浙江省数据知识产权登记中心\n 2026 年 8 月 8 日"
)

REJECTION = (
    "浙江省数据知识产权登记\n不予登记通知书\n \n乙发展有限公司:\n"
    "您于2023年08月22日提交的某预警数据数据知识产\n权登记，经形式审查，不符合《浙江省数据知识产权登记办法\n"
    "（试行）》第（五）条规定：\n1.不符合办法适用范围及原则。\n登记平台对此数据知识产权登记申请不予登记。\n"
    "浙江省知识产权研究与服务中心\n2023 年 08 月 25 日"
)

OBJECTION = (
    "浙江省数据知识产权登记\n异议转送通知书\n \n丙科技有限公司:\n"
    "您于2026年01月07日提交的bare island附近海域某分析数据数据知识产权登记申请，登记平台在公示期间，收到\n"
    "某某的异议。\n登记平台在异议期间将中止登记程序。\n浙江省数据知识产权登记中心\n2026 年 07 月 03 日"
)

NATIONAL_CERT = (
    "数\n据\n知\n识\n产\n权\n登\n记\n证\n书\n登\n记\n信\n息\n证书编号：20990000000000001\n"
    "登     记     号：SZ2099000000001.4\n数   据  名   称：某港区出运窗口安全评估数据\n"
    "登   记  主   体：甲科技有限公司\n证   件  号   码：91330000000000000X\n"
    "申   请  日   期：2026年05月28日\n登   记  日   期：2026年08月08日\n2026年08月08日"
)

PROVINCIAL_CERT = (
    " \n登记编号： DIP2099000000001\n申请人： 乙发展有限公司\n"
    "数据知识产权名称： 某实时报位信息数据\n登记日期： 2023年06月08日"
)

NOTICE = "《浙江省数据知识产权登记形式审查补正通知书》"


def test_normalize_undoes_the_layout_but_keeps_latin_word_spaces():
    flat = pdftext.normalize("您于 2026 年 8 月 28 日提交的 日度统计\n分析数据 bare island附近")
    assert flat == "您于2026年8月28日提交的日度统计分析数据bare island附近"


def test_correction_notice_deadline_dates_and_reasons():
    f = pdftext.parse(CORRECTION, NOTICE)
    assert f.kind == "补正"
    assert (f.submitted_on, f.issued_on, f.deadline) == ("2026-08-28", "2026-09-23", "2026-10-14")
    assert f.name_in_doc == "某设备运行日度统计分析数据"
    assert f.reasons.splitlines() == [
        "针对：算法规则、数据样例",
        "备注：算法规则与数据样例不适应，样例数据中“等级”的获取过程在算法规则中没有说明",
    ]
    assert f.fields == {"咨询电话": "0571-00000000", "要求": "补正修改、说明"}


def test_correction_notice_with_only_the_form_box_ticked():
    f = pdftext.parse(CORRECTION_FORM_ONLY, NOTICE)
    assert f.deadline == "2026-10-13"
    assert f.reasons.splitlines() == [
        "登记申请表填写不符合要求",
        "备注：算法规则与应用场景中不宜出现固定数据量或统计时间（数据可动态更新）",
    ]
    assert f.fields["要求"] == "补正修改"


def test_empty_remark_is_not_a_reason():
    f = pdftext.parse(CORRECTION.replace("备注:算法规则", "备注: 无 \n工作时间联系咨询:0\n算法规则"), NOTICE)
    assert f.reasons == "针对：算法规则、数据样例"


def test_announcement_certificate_validity():
    f = pdftext.parse(ANNOUNCEMENT, "《浙江省数据知识产权登记登记公告通知书》")
    assert f.kind == "公告"
    assert (f.valid_from, f.valid_to, f.issued_on) == ("2026-08-08", "2029-08-08", "2026-08-08")
    assert f.name_in_doc == "某港区出运窗口安全评估数据"


def test_rejection_reason_in_the_older_layout():
    f = pdftext.parse(REJECTION, "《浙江省数据知识产权登记不予登记通知书》")
    assert f.kind == "不予登记"
    assert (f.submitted_on, f.issued_on) == ("2023-08-22", "2023-08-25")
    assert f.name_in_doc == "某预警数据"
    assert f.reasons == "1.不符合办法适用范围及原则。"


def test_objection_names_who_objected():
    f = pdftext.parse(OBJECTION, "《浙江省数据知识产权登记异议转送通知书》")
    assert f.kind == "异议"
    assert f.reasons == "收到某某的异议"
    assert f.name_in_doc == "bare island附近海域某分析数据"


def test_national_certificate_fields():
    f = pdftext.parse(NATIONAL_CERT, "国家局登记证书")
    assert f.kind == "国家局证书"
    assert f.fields["证书编号"] == "20990000000000001"
    assert f.fields["登记号"] == "SZ2099000000001.4"
    assert f.fields["登记主体"] == "甲科技有限公司"
    assert (f.submitted_on, f.issued_on) == ("2026-05-28", "2026-08-08")
    assert f.name_in_doc == "某港区出运窗口安全评估数据"


def test_provincial_certificate_fields():
    f = pdftext.parse(PROVINCIAL_CERT, "浙江省登记证书")
    assert f.kind == "省证书"
    assert f.fields["登记编号"] == "DIP2099000000001"
    assert f.issued_on == "2023-06-08"
    assert f.name_in_doc == "某实时报位信息数据"


def test_unknown_type_and_empty_text_do_not_raise():
    assert pdftext.parse("", NOTICE).deadline is None
    assert pdftext.parse("随便什么内容", "别的文件").kind == "其他"


def test_non_pdf_and_textless_pdf_extract_to_empty(tmp_path):
    from pypdf import PdfWriter

    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf at all")
    blank = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with blank.open("wb") as fh:
        writer.write(fh)

    assert pdftext.extract_text(junk) == ""
    assert pdftext.extract_text(blank) == ""
