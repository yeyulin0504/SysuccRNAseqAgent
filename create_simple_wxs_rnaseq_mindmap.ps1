param(
    [string]$OutputDir = (Join-Path $PSScriptRoot 'outputs')
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$vsdxPath = Join-Path $OutputDir 'WXS_RNAseq_Agent_简单思维导图.vsdx'
$pngPath = Join-Path $OutputDir 'WXS_RNAseq_Agent_简单思维导图.png'

$visio = New-Object -ComObject Visio.Application
$visio.Visible = $false
$doc = $visio.Documents.Add('')
$page = $doc.Pages.Item(1)
$page.Name = '思维导图'
$page.PageSheet.CellsU('PageWidth').ResultIU = 16
$page.PageSheet.CellsU('PageHeight').ResultIU = 9

function Set-Cell($shape, [string]$cell, [string]$formula) {
    $shape.CellsU($cell).FormulaU = $formula
}

function Add-Node {
    param([double]$x,[double]$y,[double]$w,[double]$h,[string]$text,[string]$fill,[string]$line,[double]$font=11,[string]$fontColor='RGB(255,255,255)')
    $s = $page.DrawRectangle($x-$w/2,$y-$h/2,$x+$w/2,$y+$h/2)
    $s.Text = $text
    Set-Cell $s 'FillForegnd' $fill
    Set-Cell $s 'LineColor' $line
    Set-Cell $s 'LineWeight' '1.2 pt'
    Set-Cell $s 'Rounding' '0.12 in'
    Set-Cell $s 'Char.Size' "$font pt"
    Set-Cell $s 'Char.Color' $fontColor
    Set-Cell $s 'Para.HorzAlign' '1'
    Set-Cell $s 'VerticalAlign' '1'
    return $s
}

function Connect-Nodes($from,$to,[string]$color='RGB(120,130,145)') {
    $c = $page.Drop($visio.ConnectorToolDataObject,0,0)
    $c.CellsU('BeginX').GlueTo($from.CellsU('PinX'))
    $c.CellsU('EndX').GlueTo($to.CellsU('PinX'))
    Set-Cell $c 'LineColor' $color
    Set-Cell $c 'LineWeight' '1.3 pt'
    Set-Cell $c 'EndArrow' '0'
    $c.SendToBack()
}

$root = Add-Node 2.1 4.5 2.7 1.0 'WXS与RNA-seq Agent' 'RGB(31,78,121)' 'RGB(31,78,121)' 15
$mp = Add-Node 5.3 6.55 2.6 0.8 'Molecular Pathology' 'RGB(46,117,181)' 'RGB(46,117,181)' 13
$ca = Add-Node 5.3 2.45 2.6 0.8 'Clinical Application' 'RGB(112,48,160)' 'RGB(112,48,160)' 13
Connect-Nodes $root $mp
Connect-Nodes $root $ca

$wxs1 = Add-Node 8.2 7.55 1.55 0.65 'WXS' 'RGB(91,155,213)' 'RGB(91,155,213)' 12
$rna1 = Add-Node 8.2 5.55 1.55 0.65 'RNA-seq' 'RGB(91,155,213)' 'RGB(91,155,213)' 12
Connect-Nodes $mp $wxs1
Connect-Nodes $mp $rna1

$wxs2 = Add-Node 8.2 3.35 1.55 0.65 'WXS' 'RGB(165,110,205)' 'RGB(165,110,205)' 12
$rna2 = Add-Node 8.2 1.35 1.55 0.65 'RNA-seq' 'RGB(165,110,205)' 'RGB(165,110,205)' 12
Connect-Nodes $ca $wxs2 'RGB(145,100,170)'
Connect-Nodes $ca $rna2 'RGB(145,100,170)'

$lightBlue='RGB(221,235,247)'; $blueLine='RGB(91,155,213)'; $dark='RGB(40,55,70)'
$snv=Add-Node 10.9 8.15 1.45 0.55 'SNV' $lightBlue $blueLine 11 $dark
$cnv=Add-Node 12.7 7.55 1.45 0.55 'CNV' $lightBlue $blueLine 11 $dark
$indel=Add-Node 10.9 6.95 1.45 0.55 'InDel' $lightBlue $blueLine 11 $dark
Connect-Nodes $wxs1 $snv; Connect-Nodes $wxs1 $cnv; Connect-Nodes $wxs1 $indel
$exp=Add-Node 10.9 6.15 1.55 0.55 'Expression' $lightBlue $blueLine 10 $dark
$fusion=Add-Node 12.8 5.55 1.55 0.55 'Fusion' $lightBlue $blueLine 10 $dark
$splice=Add-Node 10.9 4.95 1.55 0.55 'Splicing' $lightBlue $blueLine 10 $dark
Connect-Nodes $rna1 $exp; Connect-Nodes $rna1 $fusion; Connect-Nodes $rna1 $splice

$lightPurple='RGB(235,224,244)'; $purpleLine='RGB(165,110,205)'
$guide=Add-Node 10.9 3.7 1.7 0.55 '指南收集' $lightPurple $purpleLine 10 $dark
$interpret=Add-Node 12.9 3.0 1.7 0.55 '临床解读' $lightPurple $purpleLine 10 $dark
Connect-Nodes $wxs2 $guide 'RGB(145,100,170)'; Connect-Nodes $wxs2 $interpret 'RGB(145,100,170)'
$subtype=Add-Node 10.7 1.85 1.55 0.55 '分型' $lightPurple $purpleLine 11 $dark
$response=Add-Node 10.7 0.75 1.55 0.55 '疗效预测' $lightPurple $purpleLine 10 $dark
Connect-Nodes $rna2 $subtype 'RGB(145,100,170)'; Connect-Nodes $rna2 $response 'RGB(145,100,170)'
$lit=Add-Node 13.1 2.25 1.8 0.48 '文献检索' 'RGB(247,240,251)' $purpleLine 9 $dark
$classifier=Add-Node 14.65 1.7 2.05 0.48 '分类器调用/训练' 'RGB(247,240,251)' $purpleLine 9 $dark
Connect-Nodes $subtype $lit 'RGB(145,100,170)'; Connect-Nodes $subtype $classifier 'RGB(145,100,170)'
$model=Add-Node 13.1 1.0 1.8 0.48 '模型搜索' 'RGB(247,240,251)' $purpleLine 9 $dark
$integrate=Add-Node 14.65 0.45 2.05 0.48 '成熟模型集成' 'RGB(247,240,251)' $purpleLine 9 $dark
Connect-Nodes $response $model 'RGB(145,100,170)'; Connect-Nodes $response $integrate 'RGB(145,100,170)'

$doc.SaveAs($vsdxPath)
$page.Export($pngPath)
$doc.Close()
$visio.Quit()
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($page)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($doc)
[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($visio)
Write-Output $vsdxPath
Write-Output $pngPath
