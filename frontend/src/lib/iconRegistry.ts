// Curated registry of lucide-react icons used as generator-cell glyphs.
//
// A generator-source cell carries an optional `icon` (a key from this
// registry): its cell head, its overview card and the Commands pane's
// target glyph render it. The IconPicker lets the user search/browse
// these keys. Keys are kebab-case lucide-style names so they read well in
// search and stay stable. Add to ICON_REGISTRY to widen the set — no other
// code needs to change.
import {
    Box, Boxes, Folder, FolderOpen, File, FileText, FilePenLine, Files,
    Code, CodeXml, Terminal, Cpu, Database, Server, HardDrive, Cloud,
    Globe, Link, Link2, Network, Share2, Rss,
    Sparkles, Wand, WandSparkles, Star, Zap, Flame, Lightbulb, Brain, Bot,
    Atom, FlaskConical, Beaker, Microscope, Dna, Telescope, Compass, Map, MapPin,
    MessageCircle, MessageSquare, Mail, Send, Bell, Megaphone, Mic, Languages,
    Image, ImagePlus, Camera, Video, Film, Clapperboard, Music,
    Palette, Paintbrush, PaintbrushVertical, Brush, PenTool, Pencil,
    Scissors, Combine, Merge, Split, GitBranch, GitMerge, GitPullRequest, GitFork,
    Workflow, Layers, LayoutGrid, Grid3x3, Table, List, ListChecks, Clipboard, ClipboardList,
    Search, Funnel, Eye, Scan, ScanLine, Crosshair, Target, Focus,
    Play, Pause, Square, RefreshCw, RotateCw, Repeat, Shuffle, Timer, Clock, Hourglass, Calendar,
    Settings, Settings2, SlidersHorizontal, Wrench, Hammer, Cog, Factory, Package, PackageOpen, Truck, Container,
    Scale, Gavel, Award, Trophy, Flag, Bookmark, Book, BookOpen, GraduationCap, Library, Newspaper,
    FileCode, FileBraces, FileSearch, Braces, Brackets, Variable, Regex, TestTube, Component, Blocks,
    Shield, ShieldCheck, Lock, Key, KeyRound, Fingerprint,
    User, Users, Heart, ThumbsUp,
    Activity, ChartColumn, ChartLine, ChartPie, TrendingUp, Gauge, Sigma, Hash, Binary, Calculator, Percent,
    Sun, Moon, CloudRain, Wind, Snowflake, Droplet, Leaf, Sprout, Bug, Fish, Bird,
    Rocket, Plane, Ship, Car, Anchor, Mountain, Waves, Aperture,
    Plug, PlugZap, Battery, Power, Wifi, Radio, Satellite, Gamepad2, Puzzle,
    Coffee, Gift, ShoppingCart, CreditCard, DollarSign, Coins, Wallet,
    Phone, Smartphone, Laptop, Monitor, Tv, Watch, Headphones, Keyboard, Mouse, Printer, Webcam,
    type LucideIcon,
} from 'lucide-react';

/** kebab-case key → lucide component. The user picks a key; cells store it. */
export const ICON_REGISTRY: Record<string, LucideIcon> = {
    box: Box, boxes: Boxes, folder: Folder, 'folder-open': FolderOpen,
    file: File, 'file-text': FileText, 'file-pen-line': FilePenLine, files: Files,
    code: Code, code2: CodeXml, terminal: Terminal, cpu: Cpu, database: Database,
    server: Server, 'hard-drive': HardDrive, cloud: Cloud,
    globe: Globe, link: Link, link2: Link2, network: Network, share: Share2, rss: Rss,
    sparkles: Sparkles, wand: Wand, wand2: WandSparkles, star: Star, stars: Sparkles,
    bolt: Zap, zap: Zap, flame: Flame, lightbulb: Lightbulb, idea: Lightbulb,
    brain: Brain, bot: Bot, atom: Atom, flask: FlaskConical, beaker: Beaker,
    microscope: Microscope, dna: Dna, telescope: Telescope, compass: Compass, map: Map, 'map-pin': MapPin,
    'message-circle': MessageCircle, 'message-square': MessageSquare, mail: Mail, send: Send,
    bell: Bell, megaphone: Megaphone, mic: Mic, languages: Languages,
    image: Image, 'image-plus': ImagePlus, camera: Camera, video: Video, film: Film,
    clapperboard: Clapperboard, music: Music,
    palette: Palette, paintbrush: Paintbrush, paintbrush2: PaintbrushVertical, brush: Brush,
    'pen-tool': PenTool, pencil: Pencil, edit: Pencil,
    scissors: Scissors, combine: Combine, merge: Merge, split: Split,
    git: GitBranch, 'git-branch': GitBranch, 'git-merge': GitMerge,
    'git-pull-request': GitPullRequest, 'git-fork': GitFork, workflow: Workflow,
    layers: Layers, 'layout-grid': LayoutGrid, grid: Grid3x3, table: Table,
    list: List, 'list-checks': ListChecks, clipboard: Clipboard, 'clipboard-list': ClipboardList,
    search: Search, filter: Funnel, eye: Eye, scan: Scan, 'scan-line': ScanLine,
    crosshair: Crosshair, target: Target, focus: Focus,
    play: Play, pause: Pause, square: Square, refresh: RefreshCw, rotate: RotateCw,
    repeat: Repeat, shuffle: Shuffle, timer: Timer, clock: Clock, hourglass: Hourglass, calendar: Calendar,
    settings: Settings, settings2: Settings2, sliders: SlidersHorizontal,
    wrench: Wrench, hammer: Hammer, cog: Cog, factory: Factory,
    package: Package, 'package-open': PackageOpen, truck: Truck, container: Container,
    scale: Scale, gavel: Gavel, award: Award, trophy: Trophy, flag: Flag,
    bookmark: Bookmark, book: Book, 'book-open': BookOpen, 'graduation-cap': GraduationCap,
    library: Library, newspaper: Newspaper,
    'file-code': FileCode, 'file-json': FileBraces, 'file-search': FileSearch,
    braces: Braces, brackets: Brackets, variable: Variable, regex: Regex,
    'test-tube': TestTube, component: Component, blocks: Blocks,
    shield: Shield, 'shield-check': ShieldCheck, lock: Lock, key: Key,
    'key-round': KeyRound, fingerprint: Fingerprint,
    user: User, users: Users, heart: Heart, 'thumbs-up': ThumbsUp,
    activity: Activity, 'bar-chart': ChartColumn, 'line-chart': ChartLine,
    'pie-chart': ChartPie, 'trending-up': TrendingUp, gauge: Gauge,
    sigma: Sigma, hash: Hash, binary: Binary, calculator: Calculator, percent: Percent,
    sun: Sun, moon: Moon, rain: CloudRain, wind: Wind, snowflake: Snowflake,
    droplet: Droplet, leaf: Leaf, sprout: Sprout, bug: Bug, fish: Fish, bird: Bird,
    rocket: Rocket, plane: Plane, ship: Ship, car: Car, anchor: Anchor,
    mountain: Mountain, waves: Waves, aperture: Aperture,
    plug: Plug, 'plug-zap': PlugZap, battery: Battery, power: Power, wifi: Wifi,
    radio: Radio, satellite: Satellite, gamepad: Gamepad2, puzzle: Puzzle,
    coffee: Coffee, gift: Gift, cart: ShoppingCart, 'credit-card': CreditCard,
    dollar: DollarSign, coins: Coins, wallet: Wallet,
    phone: Phone, smartphone: Smartphone, laptop: Laptop, monitor: Monitor, tv: Tv,
    watch: Watch, headphones: Headphones, keyboard: Keyboard, mouse: Mouse,
    printer: Printer, webcam: Webcam,
};

/** All registry keys, sorted — the IconPicker's browse order. */
export const ICON_NAMES: string[] = Object.keys(ICON_REGISTRY).sort();

/** Fallback glyph for a generator cell that hasn't picked an icon. */
export const DEFAULT_GROUP_ICON: LucideIcon = Boxes;

/** Resolve a stored icon key to its lucide component. Returns null for an
 *  unset / unknown key so callers can fall back to DEFAULT_GROUP_ICON (or
 *  omit the glyph). Case-insensitive. */
export function resolveIcon(name?: string | null): LucideIcon | null {
    if (!name) return null;
    const key = name.toLowerCase();
    // Own keys only: 'constructor' / '__proto__' must not resolve to Object members.
    return Object.prototype.hasOwnProperty.call(ICON_REGISTRY, key) ? ICON_REGISTRY[key] : null;
}
