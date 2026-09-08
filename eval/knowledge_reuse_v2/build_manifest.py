"""Source-only question authoring; emits files to stdout, never reads products.

The caller must apply the emitted files only after independent source-anchor
review. This module intentionally has no default path to any older task/gold.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml


SOURCE_MANIFEST = "examples/reproductions/knowledge-sources-v2/source-manifest.json"
MANIFEST = "eval/knowledge_reuse_v2.yaml"
ANSWER_KEY = "eval/knowledge_reuse_v2.answers.yaml"
GOLD_REVIEWS = (
    "eval/knowledge_reuse_v2.gold-review-a.json",
    "eval/knowledge_reuse_v2.gold-review-b.json",
)
REVIEWED_CANDIDATE_HASH = "5b889d72f5fee1b197075489e7f91768e2eec33ac2a68e616ca1e4aeb7d69821"
CJSON = "corpus/cJSON/cJSON.c"
PBUF = "corpus/lwip/src/core/pbuf.c"
IP4 = "corpus/lwip/src/core/ipv4/ip4_addr.c"
CHECKSUM = "corpus/lwip/src/core/inet_chksum.c"
KINDS = ("condition_change", "wrong_explanation", "next_verification")
BOUNDARY = (
    "AI-authored source-only questions, pending independent review; not human reviewed. "
    "Only the frozen public source, synthetic C fixtures and actual local execution records "
    "were used. No CodeAtlas Wiki, generated summary/card, older reuse gold, arm score or "
    "model gateway was consulted. Hashes bind artifacts but cannot prove lack of prior exposure. "
    "Local fixture results do not establish production reliability or universal correctness."
)


def point(text, *refs):
    return {"text": text, "refs": list(refs)}


def question(text, points, forbidden):
    return {"question": text, "points": points, "forbidden": forbidden}


# Every source interval is checked against an approved attachment before use.
# H/O/C are respectively the complete small fixture, actual observation and build
# record; more precise subranges are emitted for individual executable asserts.
CASES = {
    "cjson-parse-end": {
        "refs": {"gate": (CJSON, 1178, 1192), "error": (CJSON, 1194, 1223)},
        "condition": (CJSON, 1178, 1186), "cause": (CJSON, 1206, 1218),
        "questions": [
            question("同一缓冲区先允许前缀解析，再把 require_null_terminated 改为 1；对含 trailing 的对象和恰好三字节的 [1]，接受结果与 end 指针含义分别怎样变化？", [
                point("含 trailing 的对象在 flag=0 时成功，end_offset=7；flag=1 时失败，观测错误位置为 8。严格检查在跳过空白后要求缓冲区内存在 NUL。", "gate", "O", "H"),
                point("三字节无 NUL 的 [1] 在 flag=0 时成功，end 指向一过末尾 3；flag=1 时失败，错误位置按 length-1 限制为 2，不能把成功消费位置等同失败位置。", "gate", "error", "O")], [
                point("flag=1 只要求合法 JSON，不要求界内 NUL。", "gate"),
                point("所有情况下 end 都指向输入的一过末尾。", "error", "O")]),
            question("有人把首个成功解释为“整个缓冲区均为合法 JSON，尾部已被验证”，又把严格模式失败定位解释为“end 永远是成功消费字节数”。哪些解释被本实验和源码排除？", [
                point("前缀成功只证明对象被解析；end=7 后仍有 ' trailing'，flag=0 不执行终止符门禁，不能声称尾部已验证。", "gate", "H", "O"),
                point("失败分支报告受缓冲区边界限制的错误位置；三字节案例的 2 与非严格成功的 3 不同。", "error", "O")], [
                point("第一次成功等于完整缓冲区验证通过。", "gate", "O"),
                point("无 NUL 的三字节数组不能在任何模式下解析。", "O", "H")]),
            question("下一步如何在不越界读取 end 的前提下，用已冻结的可执行夹具复核严格与非严格解析边界？列出输入/标志矩阵、需要比较的返回值和偏移，并区分已观测结果与尚未执行的变体。", [
                point("运行固定 case 的 runner，复核 text/三字节 bounded 与 flag=0/1 的四格矩阵；比较成功/NULL 和 end-text 或 end-bounded，期望分别为成功7、失败8、成功3、失败2。", "H", "O", "C"),
                point("三字节成功的 end 是一过末尾，不得用 %s 或解引用读取它；夹具仅打印偏移。加 NUL/加尾部空白可作为后续变体，但不能冒称已运行。", "H", "gate", "error")], [
                point("可以把 bounded 的一过末尾 end 当 NUL 结尾字符串打印。", "H", "gate"),
                point("追加 NUL 或空白的新变体已经通过本次记录。", "H", "O")])]},
    "cjson-string-mutation": {
        "refs": {"gate": (CJSON, 435, 463), "grow": (CJSON, 464, 476)},
        "condition": (CJSON, 440, 449), "cause": (CJSON, 451, 463),
        "questions": [
            question("把 owned 字符串的替换内容从较短的 xy 改成长字符串，再把对象换成 CreateStringReference 或 number，返回值、存储复用和原值分别应如何判断？", [
                point("owned 缩短时返回原 valuestring 指针；增长走 strdup，成功后释放旧串并替换。测试确认内容，不应仅凭地址猜测分配是否发生。", "gate", "grow", "H", "O"),
                point("引用字符串或非字符串在门禁处返回 NULL；本例 external 与数字4保持原值，不发生所请求的字符串改写。", "gate", "H", "O")], [
                point("所有字符串节点都可用 SetValuestring 改写。", "gate", "O"),
                point("每次调用都重新分配字符串。", "gate", "H")]),
            question("“返回 NULL 一定表示内存耗尽”以及“短串和长串都只是原地 strcpy”是否能解释本例？给出源码上的不同分支和可观察反例。", [
                point("引用节点和 number 会在分配前被拒绝，因此 NULL 不能单独诊断为内存耗尽；NULL object/valuestring 和重叠输入也有拒绝条件。", "gate", "O"),
                point("只有长度不增长且不重叠的路径复用原缓冲区；长串走 strdup，分配失败在释放旧值前返回。该失败分支存在但本夹具未故障注入。", "gate", "grow", "H")], [
                point("NULL 唯一原因是 malloc/strdup 失败。", "gate"),
                point("本次已经验证分配失败时的运行时表现。", "H", "O")]),
            question("下一步应运行哪些现成断言，才能分别复核存储复用、增长后的内容和被拒绝节点未改值，而不把尚未注入的分配故障写成已验证？", [
                point("运行 runner；缩短比较 result==original 与 strcmp，增长比较非NULL和完整内容；引用节点断言返回NULL且仍为 external，数字节点返回NULL且观测值4。", "H", "O", "C"),
                point("保留各节点清理；若另做分配失败或别名输入测试，必须单独记录输入/钩子和结果，本记录没有执行这两类变体。", "H", "gate", "grow")], [
                point("仅比较打印出的字符串即可证明未分配或无内存泄漏。", "H", "gate", "grow"),
                point("当前输出已经证明所有故障注入路径。", "H", "O")])]},
    "cjson-duplicate": {
        "refs": {"node": (CJSON, 2802, 2831), "children": (CJSON, 2833, 2863)},
        "condition": (CJSON, 2827, 2831), "cause": (CJSON, 2808, 2818),
        "questions": [
            question("把 Duplicate 的 recurse 从 0 改成 1，再把源节点换成借用外部字符数组的 string reference；后续分别修改源子节点和外部字符数组，会观察到什么？", [
                point("recurse=0 复制对象节点但 child=NULL，不是与源共享 child；recurse=1 递归创建子节点，本例源数字改为9时复制值仍1且指针不同。", "node", "children", "H", "O"),
                point("复制引用字符串时清除 IsReference 并 strdup valuestring；外部 storage 改成 Borrowed 后复制串仍 borrowed，复制节点不是引用。", "node", "H", "O")], [
                point("浅复制就是共享源 child 链。", "node", "O"),
                point("引用字符串的复制仍依赖原外部存储。", "node", "O")]),
            question("“浅复制与深复制只差一个标志，都会共享孩子”和“深复制必然重新分配所有字符指针”这两种解释各有什么问题？", [
                point("浅复制在 recurse=false 时直接返回，未走 child 递归；深复制例中子节点身份不同，源修改没有影响复制数值。", "node", "children", "H", "O"),
                point("valuestring 会复制，但源码对 cJSON_StringIsConst 的键名 string 可以复用原指针，不能泛称所有字符指针均重分配。", "node")], [
                point("浅复制 child 与原 child 是同一条链。", "node", "H"),
                point("深复制对所有 const key 指针也一律 strdup。", "node")]),
            question("下一步如何用本夹具中的身份、值和引用标志断言区分三种复制行为？哪些分配失败/深度限制情形仍需另建验证？", [
                point("运行 runner，分别检查 shallow->child==NULL、original_n!=copied_n 且源改9后副本仍1、引用副本无 IsReference 且外部数组改写后内容不变。", "H", "O", "C"),
                point("保留源与副本各自 Delete；本例不覆盖内存分配失败、循环结构或递归深度上限，源码有深度限制不等于该边界已实测。", "H", "children")], [
                point("三个成功断言证明任意对象深度和分配失败都正确。", "H", "children"),
                point("可以删除外部 stack storage 来清理 reference。", "H", "node")])]},
    "cjson-replace-object": {
        "refs": {"key": (CJSON, 2427, 2448), "links": (CJSON, 2368, 2415)},
        "condition": (CJSON, 2434, 2447), "cause": (CJSON, 2370, 2373),
        "questions": [
            question("对已有 Key 的对象用 key 替换时，从 CaseSensitive API 换到普通 API 会怎样？若改为不存在的 absent，谁负责替换节点，以及失败是否保证替换节点所有字段原封不动？", [
                point("本例大小写敏感 key 不匹配 Key，返回0且原值1；普通调用返回1，新节点成为头部，键名是传入的 key，链表连接断言成立。", "key", "links", "H", "O"),
                point("absent 失败时没有转移替换节点所有权，夹具由调用者 Delete；但 helper 在查找前复制/改写 replacement->string，因此失败不保证节点所有字段未变。", "key", "links", "H", "O")], [
                point("两个 API 查找的大小写语义相同。", "H", "O"),
                point("失败会自动销毁 replacement，或保证其所有字段不变。", "key", "links", "H")]),
            question("有人依据 replaced=0 声称“replacement 已自动释放”，另有人声称“失败没有任何副作用”。对缺失键路径这两种结论能否成立？", [
                point("缺失 item 时 ViaPointer 返回false而未删除 replacement；夹具继续读取 unattached->valuedouble 并由调用者删除，排除自动释放解释。", "links", "H", "O"),
                point("helper 在 get_object_item 之前已处理 replacement->string 和 StringIsConst；失败可不改变父对象成员，却已改变替换节点键名元数据。", "key", "H")], [
                point("replaced=0 说明调用者可忘记处理 unattached。", "links", "H"),
                point("失败保证 replacement->string 与调用前相同。", "key")]),
            question("下一步如何同时验证返回值、原对象内容、替换后首尾连接与失败节点的调用者清理？哪些更广泛的链表位置测试不能由本例冒充？", [
                point("运行 runner；检查敏感失败后 Key 值1、普通成功后 object->child==replacement 且键名 key，以及 next->prev 与 child->prev 的双向连接。", "H", "O", "C"),
                point("对 absent 节点确认失败后仍由调用者删除，成功节点由 object 清理；本例只有两成员对象的头部替换，不能宣称中间/单节点等位置已经逐个执行。", "H", "links")], [
                point("应在成功后另外 Delete replacement 再 Delete object。", "links", "H"),
                point("当前夹具穷尽了所有链表位置和分配失败。", "H", "O")])]},
    "cjson-compare": {
        "refs": {"types": (CJSON, 3072, 3077), "values": (CJSON, 3117, 3152), "objects": (CJSON, 3154, 3189)},
        "condition": (CJSON, 3117, 3128), "cause": (CJSON, 3154, 3190),
        "questions": [
            question("把 case_sensitive 从 1 改为 0，Name/name 键、X/x 字符串值和 [1,2]/[2,1] 三组比较都会变相等吗？结合固定向量给出区别。", [
                point("Name/name 键在 flag=0 时相等、flag=1 时不等；但字符串值仍使用 strcmp，X/x 在 flag=0 也不等。", "objects", "values", "H", "O"),
                point("数组按顺序逐项比较，不会因为不区分对象键大小写而忽略元素顺序；[1,2] 与 [2,1] 不等。", "values", "H", "O")], [
                point("case_sensitive=0 会把所有字符串值也按大小写无关方式比较。", "values", "O"),
                point("数组和对象一样忽略顺序。", "values", "O")]),
            question("“对象比较取决于成员顺序”和“只要左边字段在右边出现，子集也算相等”是否符合本实验？数字1与字符串1又应怎样处理？", [
                point("重排对象成员的向量相等；对象双向遍历查找排除任一侧额外字段，子集/超集两个方向均不等。", "objects", "H", "O"),
                point("类型不一致先被拒绝，number 1 与 string '1' 不等，不做这种隐式类型转换。", "types", "H", "O")], [
                point("对象只检查单向子集关系。", "objects", "O"),
                point("JSON 数字和内容相同的数字字符串自动相等。", "types", "O")]),
            question("下一步应如何运行和读取现有十组比较矩阵，以分别验证键大小写、值大小写、顺序、双向额外字段和嵌套值，而不把某一个相等结果当完整证明？", [
                point("运行 runner，逐向量断言 equal==expected：重排对象1、数组重排0、键大小写0/1分别1/0、值大小写0、子集两方向0、异型0、相同嵌套1/改变嵌套0。", "H", "O", "C"),
                point("每组先确保两边 Parse 成功，再比较并各自释放；该矩阵是选定输入的回归验证，不是全部数值精度、重复键或非法节点的穷尽证明。", "H", "types", "objects")], [
                point("无需检查 Parse 返回值，NULL 比较也能代表两个合法 JSON。", "H", "types"),
                point("十组向量覆盖了所有合法和非法输入。", "H", "O")])]},
    "cjson-unicode": {
        "refs": {"hex": (CJSON, 666, 699), "pair": (CJSON, 713, 764), "encoding": (CJSON, 769, 821), "parse": (CJSON, 912, 936)},
        "condition": (CJSON, 723, 764), "cause": (CJSON, 686, 689),
        "questions": [
            question("把有效的 D834/DD1E 代理对改成孤立低代理、孤立高代理或高代理后跟0041，与改成非十六进制 ZZZZ 相比，固定版本会一律拒绝吗？", [
                point("有效代理对转换为 f09d849e；孤立低/高代理、高代理后跟非低代理及过短转义的已测输入均失败，错误偏移1。", "pair", "encoding", "H", "O"),
                point("不能一律拒绝：该版本 parse_hex4 将非法十六进制映射为0；ZZZZ 被接受为首字节NUL、strlen=0的字符串。这是固定版本观测，不是推荐合法格式。", "hex", "parse", "H", "O")], [
                point("所有非法 Unicode 转义在此版本都返回 NULL。", "hex", "O"),
                point("ZZZZ 被接受说明它是标准合法 Unicode 转义。", "hex", "H")]),
            question("最初夹具对 ZZZZ 的 NULL 断言失败，是否说明编译器坏了，或可将这个失败从记录中删除并宣称所有非法转义已拒绝？如何用源码解释并保留反例？", [
                point("初始运行 exit=-6、value==NULL 断言失败属于错误预期的保留证据；最终夹具单独断言 ZZZZ 被接受为NUL，不能抹去初始失败。", "F", "H", "O"),
                point("parse_hex4 对非法字符返回0，后续非代理分支编码该码点，解释了接受结果；仅此证据不支持归因编译器故障。", "hex", "pair", "encoding", "parse")], [
                point("最初失败已证明解析器拒绝 ZZZZ。", "F", "O"),
                point("这是已证实的编译器故障，或所有 malformed 输入均拒绝。", "hex", "H", "O")]),
            question("下一步如何重跑有效/无效代理对矩阵，并避免仅用可见字符串打印漏掉 ZZZZ 解码出的 NUL？报告应如何区分最初失败与最终观测？", [
                point("运行 runner：有效输入逐字节比对 UTF-8，无效代理/短转义检查NULL和偏移；ZZZZ单独检查字符串类型、valuestring[0]==NUL与strlen=0。", "H", "O", "C"),
                point("保留初始断言失败与最终成功记录的不同身份；已执行矩阵不代表所有十六进制字符组合或任意Unicode输入都已遍历。", "F", "H", "O")], [
                point("只看 printf 的可见文本就足够验证嵌入NUL。", "H", "O"),
                point("最终成功使初始失败记录可以删除。", "F", "O")])]},
    "lwip-address-forms": {
        "refs": {"base": (IP4, 144, 208), "width": (IP4, 221, 258), "format": (IP4, 284, 321)},
        "condition": (IP4, 163, 173), "cause": (IP4, 231, 249),
        "questions": [
            question("把127.0.0.1依次改成127.1、0x7f.1和0177.0.0.1，再把三段形式改成1.2.65535；固定解析器的接受与标准化结果怎样变化？", [
                point("前四种表示在本配置均接受并标准化为127.0.0.1；源码支持缩写及十六/八进制基数，并非只接受四段十进制。", "base", "width", "format", "H", "O"),
                point("三段形式的末段占16位，1.2.65535 标准化为1.2.255.255；不同分段数有不同位宽限制。", "width", "H", "O")], [
                point("所有段一律是0到255的十进制字节。", "base", "width", "O"),
                point("标准化地址成功证明目标网络可达。", "H", "O")]),
            question("“前导0总是十进制”和“首次链接失败说明ip4addr_aton不能解析缩写”能否解释记录？如何分别排除，并给出实际拒绝的输入？", [
                point("前导0选择八进制；08.0.0.1失败，而0177.0.0.1成功。256.1.1.1、1.2.3.256及1.2.3.4x也在当前矩阵失败。", "base", "width", "H", "O"),
                point("初始失败是缺少 _lwip_htonl 的链接依赖，并未运行解析器；最终命令补齐编译/链接单元并成功运行，因此不能用链接失败推断解析语义。", "F", "C", "O")], [
                point("08 的前导0会被忽略并按十进制8接受。", "base", "O"),
                point("Undefined symbols 已证明所有缩写输入都被解析器拒绝。", "F", "O")]),
            question("下一步如何用同一固定配置重现接受/拒绝与ntoa标准化矩阵，同时防止把构建失败当运行结果或把解析成功当联网验证？", [
                point("运行 runner，先检查所有编译/链接步骤成功，再逐输入断言 aton 的返回值；仅成功时调用 ntoa_r，并比较预期标准化文本。", "H", "C", "O"),
                point("保留初始链接失败及最终依赖记录；夹具只进行地址文本转换、没有网络操作，不能报告连通性、DHCP或真实网络兼容性已经验证。", "F", "H", "C")], [
                point("解析失败时仍可把未定义的 address 当成功结果格式化。", "H"),
                point("执行该夹具已经证明目标主机可达。", "H", "O")])]},
    "lwip-pbuf-header": {
        "refs": {"add": (PBUF, 477, 533), "remove": (PBUF, 586, 618), "force": (PBUF, 660, 664)},
        "condition": (PBUF, 501, 522), "cause": (PBUF, 599, 612),
        "questions": [
            question("对本例借用 storage 的 PBUF_REF，普通加4、减2、再减6与强制加6的结果如何变化？为什么 force 成功必须连同实际预留存储条件说明？", [
                point("普通加4失败，offset4/len7；减2成功变offset6/len5；再减6超过当前len而失败，字段保持offset6/len5。", "add", "remove", "H", "O"),
                point("force加6在本例真实storage范围内回退到offset0，len/tot_len=11；外部缓冲区force分支直接移动指针，不为调用者分配或验证外部前置容量。", "add", "force", "H", "O")], [
                point("PBUF_REF 不能执行任何负向 header 调整。", "remove", "O"),
                point("force 自动分配/保证足够外部头部空间。", "add", "H")]),
            question("“普通加头失败一定是堆内存不足，改用force总能安全修好”能否从当前源码与实验成立？请解释失败门禁和force的证据边界。", [
                point("本例普通加头因外部 PBUF_REF 且未force被拒绝，路径没有分配；减头超过当前len也会失败，不能一概归为堆耗尽。", "add", "remove", "H", "O"),
                point("force绕过外部存储的普通拒绝，直接向前移动payload；只有调用者已拥有足够合法预留空间才可据本例推断安全，未测越界force。", "add", "force", "H")], [
                point("任何加头失败都可无条件用force修复。", "add", "H"),
                point("本实验已经执行并证明越界force安全。", "H", "O")]),
            question("下一步怎样验证四次操作的返回值、payload偏移、len和tot_len，且不为验证force而构造越界指针？", [
                point("运行 runner，按顺序复核结果1/0/1/0、偏移4/6/6/0和长度7/5/5/11；同时比较日志中的tot_len，失败操作前后字段不变。", "H", "O", "C"),
                point("保留char storage与初始payload=storage+4的真实预留，最后force6从storage+6回到storage；不得从无预留地址强行回退，也不得释放栈存储。", "H", "add")], [
                point("只比较返回值就能证明payload和长度都没变。", "H", "O"),
                point("应把payload放到数组起点再force加头来证明容量安全。", "add", "H")])]},
    "lwip-pbuf-chain-ref": {
        "refs": {"cat": (PBUF, 856, 880), "chain": (PBUF, 899, 906), "ref": (PBUF, 832, 840)},
        "condition": (PBUF, 899, 906), "cause": (PBUF, 871, 879),
        "questions": [
            question("三段栈上pbuf初始ref均1：先cat前两段，再chain第三段，最后仅ref第三段。各段tot_len/ref如何变化，cat与chain的差别是什么？", [
                point("cat后first.tot_len=5，middle.tot_len=3且ref仍1；chain第三段后first/middle总长9/7，last总长4且ref=2。", "cat", "chain", "H", "O"),
                point("chain是cat后仅pbuf_ref(t)；再次ref(last)令其为3，first/middle仍1，不是给所有链节点加引用。", "chain", "ref", "H", "O")], [
                point("cat自动给尾pbuf加一次引用。", "cat", "O"),
                point("chain或ref会把链上所有节点ref一起增加。", "chain", "ref", "O")]),
            question("有人把tail_ref增加解释成“cat也隐式加引用”，又据此声称夹具已证明释放无泄漏。记录是否支持这两个结论？", [
                point("cat后的尾ref为1，chain后的新尾ref为2，随后显式ref才为3；增加由chain中的pbuf_ref及单独ref调用产生。", "cat", "chain", "ref", "H", "O"),
                point("所有节点和payload由栈提供，日志明确no_pbuf_free_called；这里只验证连接、总长和计数，未执行合法堆分配/释放，更未证明无泄漏。", "H", "O")], [
                point("当前实验通过了完整pbuf_free生命周期。", "H", "O"),
                point("尾ref=3证明cat也增加了引用。", "cat", "chain", "H")]),
            question("下一步如何重跑链连接/引用计数断言，又避免误把栈节点交给pbuf_free？若要验证真正释放生命周期，还缺什么独立实验？", [
                point("运行 runner，检查first.next、middle.next、各节点tot_len及尾ref的1→2→3，另检查前两节点ref一直1。", "H", "O", "C"),
                point("保持栈夹具不调用pbuf_free；堆分配/释放需要另建由合法pbuf分配API产生的对象并记录释放行为，当前记录不能替代。", "H", "O")], [
                point("可直接对本例栈上first调用pbuf_free做清理。", "H", "O"),
                point("引用计数断言已经等价证明无泄漏。", "H", "O")])]},
    "lwip-pbuf-copy": {
        "refs": {"copy": (PBUF, 1060, 1091), "view": (PBUF, 1110, 1138), "skip": (PBUF, 1191, 1206)},
        "condition": (PBUF, 1110, 1138), "cause": (PBUF, 1072, 1090),
        "questions": [
            question("同一三段包中，get_contiguous从单段内offset3/len2改为跨段offset2/len4，scratch为NULL或足够大时返回什么？如果请求超出包尾，和copy_partial的返回语义有何不同？", [
                point("单段内请求返回原第二段payload b（零拷贝）；跨段且无scratch返回NULL，有足够scratch则复制cdef并返回scratch。", "view", "skip", "H", "O"),
                point("copy_partial在offset7请求9只复制剩余2字节hi；get_contiguous要求完整所需len，offset8/len4返回NULL，不能把两者都当作总能满足请求。", "copy", "view", "H", "O")], [
                point("get_contiguous总会分配返回一个连续新缓冲区。", "view", "H"),
                point("copy_partial的返回数永远等于requested长度。", "copy", "O")]),
            question("“get_contiguous返回NULL就表示内存分配失败，且scratch一定未被修改”是否成立？结合跨段和越界请求解释。", [
                point("无scratch的跨段请求会直接NULL，越过包尾导致拷贝不足也会NULL；该函数没有负责分配scratch，NULL不等于malloc失败。", "view", "H", "O"),
                point("有scratch的不足长度路径先调用copy_partial再比较返回数，可能已复制部分字节；本夹具未断言失败scratch保持原样，不能补出这一保证。", "copy", "view", "H")], [
                point("所有NULL均由分配器耗尽导致。", "view", "O"),
                point("get_contiguous失败保证scratch字节完全不变。", "copy", "view")]),
            question("下一步如何用返回长度、指针身份与字节比较区分部分拷贝、零拷贝和scratch拷贝，避免用字符串终止符掩盖真实长度？", [
                point("运行 runner；copy分别断言copied=5/cdefg和copied=2/hi，在copied位置加NUL；单段view==b，跨段有scratch时view==out且memcmp四字节cdef。", "H", "O", "C"),
                point("保留无scratch和越界NULL反例；如增加scratch canary测试应检查容量和可能的部分写入，不能预设失败原子性或声称新增canary已跑。", "H", "view", "copy")], [
                point("把requested长度当copied后直接追加NUL总是安全。", "copy", "H"),
                point("本次已经验证越界返回NULL时scratch未动。", "H", "view")])]},
    "lwip-pbuf-take": {
        "refs": {"at": (PBUF, 1279, 1302), "take": (PBUF, 1235, 1266), "skip": (PBUF, 1217, 1222)},
        "condition": (PBUF, 1279, 1302), "cause": (PBUF, 1279, 1302),
        "questions": [
            question("对总长9的有效三段链，写入条件从offset2/len4变为offset8/len2、offset6/len3和offset9/len1时，返回值和包内容各如何变化？", [
                point("先跨段写WXYZ成功得到abWXYZghi；offset8/len2超出末尾返回ERR_MEM，保持本例原内容；offset6/len3恰到末尾成功得到abWXYZEND。", "at", "take", "H", "O"),
                point("offset9再写1字节返回ERR_MEM；合法链的剩余容量检查在首次复制前，不应把恰到末尾和从末尾继续写混为一谈。", "at", "skip", "H", "O")], [
                point("只要起始offset不大于tot_len，任何长度都能写入。", "at", "O"),
                point("恰好写到包尾一定失败。", "O", "H")]),
            question("“ERR_MEM只能说明分配内存失败”以及“失败总先写一部分再返回”能否解释这个有效链上的越界写？可以推广为所有畸形链的事务保证吗？", [
                point("take_at在目标pbuf不存在或剩余总长不足时直接ERR_MEM，不需要发生分配失败；当前有效链超长门禁在首次MEMCPY之前，因此两个越界例未改变包。", "at", "H", "O"),
                point("这只适用于本例一致的len/tot_len/next和有效payload；未测试损坏或不一致链，不能声称任意畸形链都有完整回滚/事务保证。", "at", "take", "H")], [
                point("本例ERR_MEM证明堆耗尽。", "at", "H"),
                point("这些断言证明任意畸形pbuf链写入都原子。", "take", "H")]),
            question("下一步如何验证跨段写入和两个拒绝操作的字节级结果，既检查完整包又不依赖一个返回码？请区分现成断言与可追加验证。", [
                point("运行 runner，复核WXYZ分别写入a尾/b/c首的memcmp，随后核对日志完整包abWXYZghi和abWXYZEND，及两次ERR_MEM时包内容不变。", "H", "O", "C"),
                point("可在独立后续变体中增加每次操作前后九字节快照memcmp和缓冲区canary；这些额外断言尚未在现有夹具执行，不能假称已经通过。", "H", "at", "take")], [
                point("返回ERR_OK就无须验证各片段字节或总长度。", "H", "O"),
                point("全包快照与canary的新增断言已有本次运行证据。", "H", "O")])]},
    "lwip-checksum-fragments": {
        "refs": {"plain": (CHECKSUM, 554, 558), "chain": (CHECKSUM, 567, 588), "standard": (CHECKSUM, 132, 173)},
        "condition": (CHECKSUM, 575, 587), "cause": (CHECKSUM, 155, 170),
        "questions": [
            question("保持九字节内容和顺序不变，把分段改成1+2+6、3+3+3、4+4+1或2+2+5，并把连续缓冲区起点改为非对齐地址，校验和应如何比较？若改成只取前八字节呢？", [
                point("四种分段与非对齐同字节输入均与连续九字节校验和相等；按夹具的字节序显示函数打印为110d，分段循环会处理奇数段带来的字节交换。", "chain", "standard", "H", "O"),
                point("前八字节改变了逻辑输入，观测为220d而不是110d，不能把重新分片等价推广成删掉最后字节也等价。", "plain", "standard", "H", "O")], [
                point("只要有奇数长度分片，校验和必定与连续输入不同。", "chain", "O"),
                point("奇数末尾字节在计算中被忽略。", "standard", "O")]),
            question("“每段独立求和直接相加就够了，不需要处理奇偶边界”和“日志110d就是所有主机上的原生u16数值”有哪些证据限制？", [
                point("pbuf循环在每段折叠并在奇数len时交换字节、切换swapped，结束后可能再交换；忽略奇偶状态无法由源码推出等价。", "chain", "standard"),
                point("夹具首先直接比较u16返回值相等，显示函数按内存两个字节组织可读值；110d是该显示约定，不应泛称所有端序主机上原生整数十六进制值均110d。", "H", "O")], [
                point("实现从不需要跨片段奇偶字节交换。", "chain"),
                point("当前打印格式证明所有机器原生u16值必为0x110d。", "H")]),
            question("下一步如何运行分片矩阵、非对齐对照和八字节负对照，验证的是相同逻辑字节而非偶然相同打印文本？这能否当作网络包端到端验证？", [
                point("运行 runner，按同一bytes数组与四种长度划分构造链，直接assert(chained==contiguous)；非对齐拷贝也直接比较返回值，并验证显示函数下八字节220d/九字节110d。", "H", "O", "C"),
                point("保留字节顺序/长度不变的条件以及改变长度的负对照；夹具没有发包，也不覆盖IP伪首部或真实网络端到端校验。", "H", "plain", "chain")], [
                point("只要输出字符串一致就无需检查原始返回值。", "H"),
                point("本次已验证真实网络或伪首部校验流程。", "H", "plain", "chain")])]},
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def checked(root, descriptor):
    path = (root / descriptor["path"]).resolve()
    if root not in path.parents:
        raise ValueError("source path escapes project")
    raw = path.read_bytes()
    if sha(raw) != descriptor["sha256"]:
        raise ValueError(f"source hash mismatch: {descriptor['path']}")
    return raw.decode("utf-8")


def line_text(text, start, end):
    lines = text.splitlines(keepends=True)
    if not 1 <= start <= end <= len(lines):
        raise ValueError("invalid exact line interval")
    return "".join(lines[start - 1:end])


def fact(tag, whole, selected):
    selected = selected.strip()
    start = whole.index(selected)
    return {"text": selected, "evidence_tags": [tag],
            "source_spans": [{"tag": tag, "start": start, "end": start + len(selected)}]}


def build(root, *, expected_source_hash=None):
    """Return deterministic new artifacts; no files, model clients or DB writes."""
    root = Path(root).resolve()
    raw_catalog = (root / SOURCE_MANIFEST).read_bytes()
    catalog_hash = sha(raw_catalog)
    if expected_source_hash and expected_source_hash != catalog_hash:
        raise ValueError("independently reviewed source manifest hash changed")
    catalog = json.loads(raw_catalog)
    if catalog.get("schema_version") != 2 or catalog.get("case_count") != 12:
        raise ValueError("expected the twelve-case source-only schema 2 catalog")
    if {c["id"] for c in catalog["cases"]} != set(CASES):
        raise ValueError("source cases differ from authored questions")
    files, cases, tasks, answers = {}, [], [], {}
    for count in (3, 4):
        layout = {"generic_summary": {"turn_ids": list(range(1, count + 1))},
                  "structured_card": [{"heading": "现象", "turn_ids": [1]},
                                      {"heading": "验证记录", "turn_ids": list(range(2, count + 1))}]}
        files[f"eval/knowledge_reuse_v2/layout-{count}.yaml"] = yaml.safe_dump(layout, allow_unicode=True, sort_keys=False)
    for original in catalog["cases"]:
        cid, spec = original["id"], CASES[original["id"]]
        session_text = checked(root, original["session"])
        session = json.loads(session_text)
        turns = {f"T{i}": m["content"] for i, m in enumerate(session["messages"], 1)}
        by_kind, attachments, refs = {}, [], {}
        for i, descriptor in enumerate(original["attachments"], 1):
            whole = checked(root, descriptor)
            start = descriptor.get("line_start", 1)
            end = descriptor.get("line_end", len(whole.splitlines()))
            item = {**descriptor, "tag": f"E{i}", "text": line_text(whole, start, end),
                    "line_start": start, "line_end": end, "whole": whole}
            attachments.append(item)
            by_kind.setdefault(descriptor["kind"], []).append(item)

        def evidence(name, item, start=None, end=None):
            start = item["line_start"] if start is None else start
            end = item["line_end"] if end is None else end
            text = line_text(item["whole"], start, end)
            offset = len(line_text(item["whole"], item["line_start"], start - 1)) if start > item["line_start"] else 0
            refs[name] = {"id": name, "tag": item["tag"], "kind": item["kind"],
                          "path": item["path"], "file_sha256": item["sha256"],
                          "repository": original["repository"], "revision": original["revision"],
                          "line_start": start, "line_end": end, "text": text,
                          "text_sha256": sha(text.encode()),
                          "source_span": {"tag": item["tag"], "start": offset, "end": offset + len(text)}}
            return refs[name]

        def source_ref(name, location):
            path, start, end = location
            matches = [a for a in attachments if a["path"] == path and a["kind"] == "source"
                       and a["line_start"] <= start <= end <= a["line_end"]]
            if len(matches) != 1:
                raise ValueError(f"exact approved source interval missing or ambiguous: {cid}/{name}")
            return evidence(name, matches[0], start, end)

        for name, location in spec["refs"].items():
            source_ref(name, location)
        for name, kind in (("H", "harness"), ("O", "observation"), ("C", "command")):
            if len(by_kind[kind]) != 1:
                raise ValueError("one exact fixture/observation/command expected")
            evidence(name, by_kind[kind][0])
        harness = refs["H"]["text"]
        assertion_lines = [(i, line.strip()) for i, line in enumerate(harness.splitlines(), 1) if "assert(" in line]
        # Exact minimal statements, not authored conclusions, populate the
        # production contract. All original turns remain explicitly covered.
        condition = source_ref("condition_fact", spec["condition"])
        cause = source_ref("cause_fact", spec["cause"])
        observation = json.loads(refs["O"]["text"])
        stdout_literal = json.dumps(observation["stdout"], ensure_ascii=False)
        facts = {
            "symptom": [fact("T3", turns["T3"], stdout_literal)],
            "hypotheses": [fact("T1", turns["T1"], assertion_lines[0][1])],
            "conditions": [fact(condition["tag"], next(a["text"] for a in attachments if a["tag"] == condition["tag"]), condition["text"])],
            "wrong_explanations": [fact("T1", turns["T1"], assertion_lines[-1][1])],
            "root_cause": [fact(cause["tag"], next(a["text"] for a in attachments if a["tag"] == cause["tag"]), cause["text"])],
            "handling": [fact("T1", turns["T1"], assertion_lines[len(assertion_lines) // 2][1])],
            "verification": [fact("T2", turns["T2"], '"exit_code": 0'), fact("T3", turns["T3"], '"exit_code": 0')],
        }
        if "T4" in turns:
            failure = turns["T4"].split("\n\nInitial failed fixture", 1)[0].strip()
            facts["verification"].append(fact("T4", turns["T4"], failure))
            refs["F"] = {"id": "F", "tag": "T4", "kind": "preserved_initial_failure",
                         "path": original["session"]["path"], "file_sha256": original["session"]["sha256"],
                         "json_pointer": "/messages/3/content", "text": failure,
                         "text_sha256": sha(failure.encode()),
                         "source_span": facts["verification"][-1]["source_spans"][0]}
        template_path = f"eval/knowledge_reuse_v2/layout-{len(turns)}.yaml"
        cases.append({"id": cid, "repository": original["repository"], "revision": original["revision"],
                      "corpus": original["corpus"], "mechanism_id": cid,
                      "length_bucket": original["length_bucket"], "session": copy.deepcopy(original["session"]),
                      "template": {"path": template_path, "sha256": sha(files[template_path].encode())},
                      "attachments": copy.deepcopy(original["attachments"]), "required_facts": facts,
                      "execution": copy.deepcopy(original["execution"]),
                      "initial_failure_records": copy.deepcopy(original["initial_failure_records"])})
        assertion_refs = []
        for i, (line, _) in enumerate(assertion_lines, 1):
            name = f"A{i}"
            evidence(name, by_kind["harness"][0], line, line)
            assertion_refs.append(name)
        for kind, q in zip(KINDS, spec["questions"], strict=True):
            tid = f"{cid}.{kind}"
            tasks.append({"id": tid, "case_id": cid, "mechanism_id": cid, "question_kind": kind,
                          "question": q["question"], "type": "经验同源对照"})
            used = set(assertion_refs) | {"H", "O", "C"}
            expected = []
            for i, p in enumerate(q["points"], 1):
                used.update(p["refs"])
                expected.append({"id": f"P{i}", "text": p["text"], "evidence": p["refs"]})
            forbidden = []
            for i, p in enumerate(q["forbidden"], 1):
                used.update(p["refs"])
                forbidden.append({"claim_index": i - 1, "evidence": p["refs"]})
            if not used <= refs.keys():
                raise ValueError(f"unknown gold source: {tid}: {used - refs.keys()}")
            answers[tid] = {"expected_points": expected,
                            "forbidden_claims": [p["text"] for p in q["forbidden"]],
                            "forbidden_evidence": forbidden,
                            "evidence_payload": [refs[name] for name in sorted(used)],
                            "executable_verification": {
                                "command": [".venv/bin/python", "-B", "examples/reproductions/knowledge-sources-v2/run.py", cid],
                                "cwd": "PROJECT_ROOT", "expected_exit_code": 0,
                                "assertions": [{"text": text, "evidence": [name]} for name, (_, text) in zip(assertion_refs, assertion_lines, strict=True)],
                                "observation_evidence": ["O"], "build_evidence": ["C"],
                                "status": "captured_source_fixture; authoring_rerun_not_claimed",
                                "proposed_variants": "Any additional variants described in expected points are proposed, not recorded executions."},
                            "reviewer_kind": "agent", "status": "ai_reviewed",
                            "review_status": "pending_independent_review"}
    manifest = {"schema_version": 2, "artifact_scope": "isolated_experiment", "strip_knowledge_role": True,
                "status": "ai_reviewed", "review_status": "pending_independent_review", "reviewer_kind": "agent",
                "source_manifest": {"path": SOURCE_MANIFEST, "sha256": catalog_hash},
                "authoring_boundary": BOUNDARY, "case_count": 12, "question_count": 36,
                "cases": cases, "tasks": tasks}
    files[MANIFEST] = yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False, width=100)
    key = {"schema_version": 2, "task_set_hash": sha(files[MANIFEST].encode()),
           "source_manifest_hash": catalog_hash, "split": "held_out",
           "status": "ai_reviewed", "review_status": "pending_independent_review", "reviewer_kind": "agent",
           "frozen_at": "2026-09-08", "not_used_for_tuning": True,
           "authoring_boundary": BOUNDARY,
           "independent_review": {"status": "pending", "human_reviewed": False, "reviewer_kind": None},
           "tasks": answers}
    # The question author emits a pending candidate first.  Promotion is only
    # reproducible when two separately stored reviews bind those exact bytes and
    # the semantic input hash used by the shared evaluation protocol.
    candidate_text = yaml.safe_dump(key, allow_unicode=True, sort_keys=False, width=100)
    candidate_hash = sha(candidate_text.encode())
    if candidate_hash != REVIEWED_CANDIDATE_HASH:
        raise ValueError("reviewed answer-key candidate changed")
    from codeatlas.eval.protocol import answer_key_input_hash
    review_input_hash = answer_key_input_hash(key)
    review_records, review_files, agents = [], [], set()
    for review_path in GOLD_REVIEWS:
        path = (root / review_path).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("independent gold review is missing")
        raw = path.read_bytes()
        review = json.loads(raw)
        record = review.get("review_record") or {}
        if (review.get("artifact_kind") != "knowledge_reuse_gold_review"
                or review.get("reviewed_answer_key_sha256") != candidate_hash
                or review.get("manifest_sha256") != sha(files[MANIFEST].encode())
                or review.get("source_manifest_sha256") != catalog_hash
                or review.get("human_reviewed") is not False
                or record.get("reviewer_kind") != "agent"
                or record.get("input_hash") != review_input_hash
                or record.get("verdict") != "approved"
                or record.get("independent") is not True
                or record.get("peer_reviews_visible") is not False):
            raise ValueError("independent gold review does not bind the frozen candidate")
        agents.add(record.get("agent"))
        review_records.append(record)
        review_files.append({"path": review_path, "sha256": sha(raw)})
    if len(agents) != 2:
        raise ValueError("gold approval requires two distinct AI reviewers")
    approved_tasks = key.pop("tasks")
    key.update(
        status="approved",
        review_status="ai_reviewed",
        reviewer_kind="agent",
        independent_review={"status": "ai_reviewed", "human_reviewed": False,
                            "reviewer_kind": "agent", "review_files": review_files},
    )
    key["review"] = {"reviewer_kind": "agent", "independent_reviews": review_records}
    key["tasks"] = approved_tasks
    files[ANSWER_KEY] = yaml.safe_dump(key, allow_unicode=True, sort_keys=False, width=100)
    return files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--expected-source-hash", required=True,
                        help="Explicit independently reviewed source identity; emits no files without it")
    args = parser.parse_args()
    print(json.dumps(build(args.project_root, expected_source_hash=args.expected_source_hash), ensure_ascii=False))
