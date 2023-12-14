<?xml version="1.0" encoding="utf-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:msxsl="urn:schemas-microsoft-com:xslt" exclude-result-prefixes="msxsl">

	<xsl:output method="xml" indent="yes"/>

	<xsl:template
	  match="@*[namespace-uri()!='' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2015' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2016' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2018']" />

	<xsl:template
	  match="node()[namespace-uri(.)!='' and namespace-uri(.)!='http://www.eos.ru/2010/sev' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2015' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2016' and namespace-uri(.)!='http://www.eos.ru/2010/sev/Ext2018']" />

	<xsl:template match="@* | node()">
		<xsl:copy>
			<xsl:apply-templates select="@* | node() "/>
		</xsl:copy>
	</xsl:template>


</xsl:stylesheet>
