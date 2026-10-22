/* 測試檔 #056：9 個 JOIN、53 個欄位 */
SELECT
    t7.BRANCH_SEQ,
    t4.c001 AS 對帳名稱,
    t5.c002 分行編號,
    t1.FX_SEQ,
    t4.c004 AS [櫃員日期],
    [交易代碼] = t6.c005,
    t5.c006 客戶號碼,
    t7.c007 AS [放款號碼],
    t0.c008 放款金額,
    t8.c009 LEDGER_CODE,
    t3.c010 AS [帳戶序號],
    t4.c011 AS [外幣類別],
    t5.c012 AS "分行序號",
    t4.c013 AS [匯款狀態],
    t1.c014 AS [FX_CODE],
    t6.c015 帳戶ID,
    [櫃員編號] = t8.c016,
    t6.c017 AS [對帳類別],
    t8.c018 AS "REMIT_DATE",
    [LEDGER_NAME] = t4.c019,
    [分行狀態] = t4.c020,
    t3.CARD_CODE,
    t5.BRANCH_BAL,
    t8.c023 客戶類別,
    t2.c024 交易編號,
    t7.c025 AS "LOAN_ID",
    t6.c026 分行ID,
    t8.c027 AS [定存序號],
    t3.c028 AS "DEPOSIT_BAL",
    t7.c029 AS 欄29 /* 對應 TELLER_BAL */,
    t7.c030 AS [ACCOUNT_TYPE],
    t8.c031 TELLER_TYPE,
    t8.c032 AS 欄32 /* 對應 存款號碼 */,
    t9.c033 AS [TD_DATE],
    t3.c034 AS "RATE_TYPE",
    [客戶日期] = t6.c035,
    t8.c036 AS "利率序號",
    t4.c037 AS [TXN_CODE],
    t3.c038 存款餘額,
    t9.c039 AS [交易日期],
    t8.c040 AS TXN_NO,
    t5.c041 AS 櫃員代碼,
    t4.c042 AS [CUSTOMER_STATUS],
    t0.c043 AS [帳戶名稱],
    [BRANCH_CODE] = t0.c044,
    [匯款編號] = t2.c045,
    t4.c046 TD_NAME,
    t7.c047 AS "CHANNEL_ID",
    t0.c048 AS 受款人編號,
    t7.c049 AS "PAYEE_CODE",
    t4.c050 AS "存款序號",
    t9.c051 AS "放款序號",
    t9.c052 AS CARD_AMT,
    t7.c999 AS "交易餘額_舊",
    t4.c999 AS [客戶IDOLD],
    [舊放款名稱] = t3.c999,
    t6.c999 AS "舊存款號碼",
    t5.c999 AS 分行CODE2
FROM ACCT_MASTER_5 t0
LEFT OUTER JOIN BANK_TBL_3 t1
    ON t0.k01 = t1.k01
LEFT JOIN BANK_TBL_45 t2
    ON t0.k02 = t2.k02
INNER JOIN BANK_TBL_20 t3
    ON t0.k03 = t3.k03
LEFT OUTER JOIN BANK_TBL_2 t4
    ON t0.k04 = t4.k04
LEFT JOIN BANK_TBL_36 t5
    ON t0.k05 = t5.k05
INNER JOIN BANK_TBL_20 t6
    ON t0.k06 = t6.k06
LEFT JOIN BANK_TBL_22 t7
    ON t0.k07 = t7.k07
LEFT JOIN BANK_TBL_57 t8
    ON t0.k08 = t8.k08
INNER JOIN BANK_TBL_9 t9
    ON t0.k09 = t9.k09
WHERE t0.acct_status = '正常' AND t0.c000 >= :p_from_dt
